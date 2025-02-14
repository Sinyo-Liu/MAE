import torch
import torch.nn as nn
import torch.nn.functional as F
import timm
from timm.models.layers import DropPath, trunc_normal_
import numpy as np
from .build import MODELS
from utils import misc
from utils.checkpoint import get_missing_parameters_message, get_unexpected_parameters_message
from utils.logger import *
import random
# from knn_cuda import KNN  # this is slower than the knn_point function empirically
from utils.knn import knn_point
from extensions.chamfer_dist import ChamferDistanceL1, ChamferDistanceL2
from models.pos import get_pos_embed
from torch_scatter import scatter
import clip



class Encoder(nn.Module):   ## Embedding module
    def __init__(self, encoder_channel):
        super().__init__()
        self.encoder_channel = encoder_channel
        self.first_conv = nn.Sequential(
            nn.Conv1d(3, 128, 1),
            nn.BatchNorm1d(128),
            nn.ReLU(inplace=True),
            nn.Conv1d(128, 256, 1)
        )
        self.second_conv = nn.Sequential(
            nn.Conv1d(512, 512, 1),
            nn.BatchNorm1d(512),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, self.encoder_channel, 1)
        )

    def forward(self, point_groups):
        '''
            point_groups : B G N 3
            -----------------
            feature_global : B G C
        '''
        bs, g, n , _ = point_groups.shape
        point_groups = point_groups.reshape(bs * g, n, 3)
        # encoder
        feature = self.first_conv(point_groups.transpose(2,1))  # BG 256 n
        feature_global = torch.max(feature,dim=2,keepdim=True)[0]  # BG 256 1
        feature = torch.cat([feature_global.expand(-1,-1,n), feature], dim=1)# BG 512 n
        feature = self.second_conv(feature) # BG 1024 n
        feature_global = torch.max(feature, dim=2, keepdim=False)[0] # BG 1024
        return feature_global.reshape(bs, g, self.encoder_channel)



class Group(nn.Module):  # FPS + KNN
    def __init__(self, num_group, group_size):
        super().__init__()
        self.num_group = num_group
        self.group_size = group_size
        # self.knn = KNN(k=self.group_size, transpose_mode=True)

    def forward(self, xyz):
        '''
            input: B N 3
            ---------------------------
            output: B G M 3
            center : B G 3
        '''
        batch_size, num_points, _ = xyz.shape
        # fps the centers out
        center = misc.fps(xyz, self.num_group) # B G 3
        # knn to get the neighborhood
        # _, idx = self.knn(xyz, center) # B G M
        idx = knn_point(self.group_size, xyz, center)
        assert idx.size(1) == self.num_group
        assert idx.size(2) == self.group_size
        idx_base = torch.arange(0, batch_size, device=xyz.device).view(-1, 1, 1) * num_points
        idx = idx + idx_base
        idx = idx.view(-1)
        neighborhood = xyz.view(batch_size * num_points, -1)[idx, :]
        neighborhood = neighborhood.view(batch_size, self.num_group, self.group_size, 3).contiguous()
        # normalize
        neighborhood = neighborhood - center.unsqueeze(2)
        return neighborhood, center


## Transformers
class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


# Shared weight self-attention and cross attention
class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    # def forward(self, x):
    #     B, N, C = x.shape
    #     qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
    #     q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

    #     attn = (q @ k.transpose(-2, -1)) * self.scale
    #     attn = attn.softmax(dim=-1)
    #     attn = self.attn_drop(attn)

    #     x = (attn @ v).transpose(1, 2).reshape(B, N, C)
    #     x = self.proj(x)
    #     x = self.proj_drop(x)
    #     return x
    
    def forward(self, x, y=None):    # y as q, x as q, k, v
        if y is None:
            # Self attention
            B, N, C = x.shape
            qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            attn = self.attn_drop(attn)

            x = (attn @ v).transpose(1, 2).reshape(B, N, C)
            x = self.proj(x)
            x = self.proj_drop(x)
            return x
        
        # Self attention + Cross attention
        B, N, C = x.shape
        L = y.shape[1]
        x = torch.cat([x, y], dim=1) 
        qkv = self.qkv(x).reshape(B, N+L, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        # q: B, num_heads, N+L, C//num_heads

        # Cross attention
        # y query
        attn = (q[:, :, N:] @ k[:, :, :].transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        y = (attn @ v[:, :, :]).transpose(1, 2).reshape(B, L, C)
        y = self.proj(y)
        y = self.proj_drop(y)

        # Self attention
        attn = (q[:, :, :N] @ k[:, :, :N].transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v[:, :, :N]).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x, y # , attn


class Block(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)

        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        
    # def forward(self, x, y):    # y is q
    #     x = x + self.drop_path(self.attn(self.norm1(x))) 
    #     x = x + self.drop_path(self.mlp(self.norm2(x)))
    #     return x
    def forward(self, x, y=None):    # y is q
        if y is None:
            x = x + self.drop_path(self.attn(self.norm1(x))) 
            x = x + self.drop_path(self.mlp(self.norm2(x)))
            return x
        new_x = self.norm1(x)
        new_y = self.norm1(y)
        
        new_x, new_y = self.attn(new_x, new_y)
        new_x = x + self.drop_path(new_x)
        new_y = y + self.drop_path(new_y)
        
        new_x = new_x + self.drop_path(self.mlp(self.norm2(new_x)))
        new_y = new_y + self.drop_path(self.mlp(self.norm2(new_y)))
        return new_x, new_y

class SelfAttention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

class SelfBlock(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)

        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

        self.attn = SelfAttention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        
    def forward(self, x):
        x = x + self.drop_path(self.attn(self.norm1(x)))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x
    

class TransformerEncoder(nn.Module):
    def __init__(self, embed_dim=768, depth=4, num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.):
        super().__init__()
        
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, 
                drop_path = drop_path_rate[i] if isinstance(drop_path_rate, list) else drop_path_rate
                )
            for i in range(depth)])

    def forward(self, x, pos, x_mask=None, pos_mask=None):
        if x_mask is None:
            for _, block in enumerate(self.blocks):
                x = block(x + pos)
            return x
        else:    
            for _, block in enumerate(self.blocks):
                x, x_mask = block(x + pos, x_mask + pos_mask)      
            return x, x_mask


class TransformerDecoder(nn.Module):
    def __init__(self, embed_dim=384, depth=4, num_heads=6, mlp_ratio=4., qkv_bias=False, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1, norm_layer=nn.LayerNorm):
        super().__init__()
        self.blocks = nn.ModuleList([
            SelfBlock(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate,
                drop_path=drop_path_rate[i] if isinstance(drop_path_rate, list) else drop_path_rate
            )
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)
        self.head = nn.Identity()

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x, pos=None, return_token_num=None):
        if pos is None:
            # pred pos decoder
            for _, block in enumerate(self.blocks):
                x = block(x)
            x = self.head(self.norm(x))
            return x         
        for _, block in enumerate(self.blocks):
            x = block(x + pos)

        x = self.head(self.norm(x[:, -return_token_num:]))  # only return the mask tokens predict pixel
        return x



class TransformerCrossDecoder(nn.Module):
    def __init__(self, embed_dim=384, depth=4, num_heads=6, mlp_ratio=4., qkv_bias=False, qk_scale=None,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1, norm_layer=nn.LayerNorm):
        super().__init__()
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate,
                drop_path=drop_path_rate[i] if isinstance(drop_path_rate, list) else drop_path_rate
            )
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)
        self.head = nn.Identity()

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x, y, pos=None, return_token_num=None):
        if pos is None:
            # pred pos decoder
            for _, block in enumerate(self.blocks):
                x, _ = block(x, y)
                # x = block(x)
            x = self.head(self.norm(x))
            return x         
        for _, block in enumerate(self.blocks):
            # x = block(x + pos)
            x, _ = block(x + pos, y)

        x = self.head(self.norm(x[:, -return_token_num:]))  # only return the mask tokens predict pixel
        return x




# Pretrain model
class MaskTransformer(nn.Module):
    def __init__(self, config, **kwargs):
        super().__init__()
        self.config = config
        # define the transformer argparse
        self.mask_ratio = config.transformer_config.mask_ratio 
        self.trans_dim = config.transformer_config.trans_dim
        self.depth = config.transformer_config.depth 
        self.drop_path_rate = config.transformer_config.drop_path_rate
        self.num_heads = config.transformer_config.num_heads 
        print_log(f'[args] {config.transformer_config}', logger = 'Transformer')
        # embedding
        self.encoder_dims =  config.transformer_config.encoder_dims
        self.encoder = Encoder(encoder_channel = self.encoder_dims)

        self.mask_type = config.transformer_config.mask_type
        self.mask_pos_token = nn.Parameter(torch.zeros(1, 1, self.trans_dim))
        trunc_normal_(self.mask_pos_token, std=.02)

        self.pos_embed = nn.Sequential(
            nn.Linear(self.trans_dim, self.trans_dim),
            nn.GELU(),
            nn.Linear(self.trans_dim, self.trans_dim),
        )

        dpr = [x.item() for x in torch.linspace(0, self.drop_path_rate, self.depth)]
        self.blocks = TransformerEncoder(
            embed_dim = self.trans_dim,
            depth = self.depth,
            drop_path_rate = dpr,
            num_heads = self.num_heads,
        )

        self.norm = nn.LayerNorm(self.trans_dim)
        
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv1d):
            trunc_normal_(m.weight, std=.02)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def _mask_center_block(self, center, noaug=False):
        '''
            center : B G 3
            --------------
            mask : B G (bool)
        '''
        # skip the mask
        if noaug or self.mask_ratio == 0:
            return torch.zeros(center.shape[:2]).bool()
        # mask a continuous part
        mask_idx = []
        for points in center:
            # G 3
            points = points.unsqueeze(0)  # 1 G 3
            index = random.randint(0, points.size(1) - 1)
            distance_matrix = torch.norm(points[:, index].reshape(1, 1, 3) - points, p=2,
                                         dim=-1)  # 1 1 3 - 1 G 3 -> 1 G

            idx = torch.argsort(distance_matrix, dim=-1, descending=False)[0]  # G
            ratio = self.mask_ratio
            mask_num = int(ratio * len(idx))
            mask = torch.zeros(len(idx))
            mask[idx[:mask_num]] = 1
            mask_idx.append(mask.bool())

        bool_masked_pos = torch.stack(mask_idx).to(center.device)  # B G

        return bool_masked_pos

    def _mask_center_rand(self, center, noaug = False):
        '''
            center : B G 3
            --------------
            mask : B G (bool)
        '''
        B, G, _ = center.shape
        # skip the mask
        if noaug or self.mask_ratio == 0:
            return torch.zeros(center.shape[:2]).bool()

        self.num_mask = int(self.mask_ratio * G)

        overall_mask = np.zeros([B, G])
        for i in range(B):
            mask = np.hstack([
                np.zeros(G-self.num_mask),
                np.ones(self.num_mask),
            ])
            np.random.shuffle(mask)
            overall_mask[i, :] = mask
        overall_mask = torch.from_numpy(overall_mask).to(torch.bool)

        return overall_mask.to(center.device) # B G

    def forward(self, neighborhood, center, noaug = False):
        # generate mask
        if self.mask_type == 'rand':
            bool_masked_pos = self._mask_center_rand(center, noaug = noaug) # B G
        else:
            bool_masked_pos = self._mask_center_block(center, noaug = noaug)
                            
        
        group_input_tokens = self.encoder(neighborhood)  #  B G C

        batch_size, seq_len, C = group_input_tokens.size()

        x_vis = group_input_tokens[~bool_masked_pos].reshape(batch_size, -1, C)
        x_mask = group_input_tokens[bool_masked_pos].reshape(batch_size, -1, C)
        # add pos embedding
        # mask pos center
        vis_center = center[~bool_masked_pos].reshape(batch_size, -1, 3)
        pos = self.pos_embed(get_pos_embed(self.trans_dim, vis_center))

        # transformer
        M = x_mask.shape[1]
        mask_pos_token = self.mask_pos_token.expand(batch_size, M, self.trans_dim)
        
        x_vis, x_mask = self.blocks(x_vis, pos, x_mask, mask_pos_token)
        x_vis = self.norm(x_vis)
        x_mask = self.norm(x_mask)
        
        return x_vis, x_mask, bool_masked_pos


@MODELS.register_module()
class PCP_MAE(nn.Module):
    def __init__(self, config):
        super().__init__()
        print_log(f'[PCP_MAE] ', logger ='PCP_MAE')
        self.config = config
        self.trans_dim = config.transformer_config.trans_dim
        self.MAE_encoder = MaskTransformer(config)
        self.group_size = config.group_size
        self.num_group = config.num_group
        self.drop_path_rate = config.transformer_config.drop_path_rate
        self.mask_token = nn.Parameter(torch.zeros(1, 1, self.trans_dim))

        self.decoder_depth = config.transformer_config.decoder_depth
        self.decoder_num_heads = config.transformer_config.decoder_num_heads
        dpr = [x.item() for x in torch.linspace(0, self.drop_path_rate, self.decoder_depth)]
        self.MAE_decoder = TransformerDecoder(
            embed_dim=self.trans_dim,
            depth=self.decoder_depth,
            drop_path_rate=dpr,
            num_heads=self.decoder_num_heads,
        )

        self.MAE_Cross_decoder = TransformerCrossDecoder(
            embed_dim=self.trans_dim,
            depth=self.decoder_depth,
            drop_path_rate=dpr,
            num_heads=self.decoder_num_heads,
        )


        print_log(f'[PCP_MAE] divide point cloud into G{self.num_group} x S{self.group_size} points ...', logger ='PCP_MAE')
        self.group_divider = Group(num_group = self.num_group, group_size = self.group_size)

        # prediction head
        self.increase_dim = nn.Sequential(
            # nn.Conv1d(self.trans_dim, 1024, 1),
            # nn.BatchNorm1d(1024),
            # nn.LeakyReLU(negative_slope=0.2),
            nn.Conv1d(self.trans_dim, 3*self.group_size, 1)
        )

        self.increase_cross_dim = nn.Sequential(
            # nn.Conv1d(self.trans_dim, 1024, 1),
            # nn.BatchNorm1d(1024),
            # nn.LeakyReLU(negative_slope=0.2),
            nn.Conv1d(self.trans_dim, 3*self.group_size, 1)
        )

        trunc_normal_(self.mask_token, std=.02)
        self.loss = config.loss
        # loss
        self.build_loss_func(self.loss)
        
        # self.pred_pos_proj = nn.Sequential( # input B, M, C  
        #     nn.Linear(self.trans_dim, self.trans_dim),
        #     nn.LayerNorm(self.trans_dim),
        #     nn.ReLU(inplace=True),
        #     nn.Linear(self.trans_dim, self.trans_dim),
        # )
        self.pred_pos_proj = nn.Sequential( # input B, M, C  
            nn.Linear(self.trans_dim, 512),
            nn.LayerNorm(512),
            nn.ReLU(inplace=True),
            nn.Linear(512, 512),
        )  
        
        self.pred_loss = config.pred_loss
        if self.config.pred_pos_transformer_layer != 0:
            dpr = [x.item() for x in torch.linspace(0, self.drop_path_rate, self.config.pred_pos_transformer_layer)]
            self.pred_pos_decoder = TransformerDecoder(
                embed_dim=self.trans_dim,
                depth=self.config.pred_pos_transformer_layer,
                drop_path_rate=dpr,
                num_heads=self.decoder_num_heads,
            )
        
        self.add_detach = config.add_detach

        # 2D pre-trained models, clip by default
        self.clip_model, _ = clip.load(config.clip_config.visual_encoder)
        self.clip_model.eval()

        # multi-view projection
        self.img_mean = torch.Tensor([0.485, 0.456, 0.406])
        self.img_std = torch.Tensor([0.229, 0.224, 0.225])
        self.img_size = 224
        self.img_offset = torch.Tensor([[-2, -2], [-2, -1], [-2, 0], [-2, 1], [-2, 2], 
                                        [-1, -2], [-1, -1], [-1, 0], [-1, 1], [-1, 2], 
                                        [0, -2], [0, -1], [0, 0], [0, 1], [0, 2],
                                        [1, -2], [1, -1], [1, 0], [1, 1], [1, 2],
                                        [2, -2], [2, -1], [2, 0], [2, 1], [2, 2]])
        self.proj_reduction = 'sum'

    ''' Efficient Projection '''
    def proj2img(self, pc):
        B, N, _ = pc.shape
        
        # calculate range
        pc_range = pc.max(dim=1)[0] - pc.min(dim=1)[0]  # B 3
        grid_size = pc_range[:, :2].max(dim=-1)[0] / (self.img_size - 3)  # B,

        # Point Index
        pc_min = pc.min(dim=1)[0][:, :2].unsqueeze(dim=1)
        grid_size = grid_size.unsqueeze(dim=1).unsqueeze(dim=2)
        idx_xy = torch.floor((pc[:, :, :2] - pc_min) / grid_size)  # B N 2
        
        # Point Densify
        idx_xy_dense = (idx_xy.unsqueeze(dim=2) + self.img_offset.unsqueeze(dim=0).unsqueeze(dim=0).to(pc.device)).view(idx_xy.size(0), N*25, 2) + 1
        # B N 1 2 + 1 1 9 2 -> B N 9 2 -> B 9N 2
        
        # Object to Image Center
        idx_xy_dense_center = torch.floor((idx_xy_dense.max(dim=1)[0] + idx_xy_dense.min(dim=1)[0]) / 2).int()
        offset_x = self.img_size / 2 - idx_xy_dense_center[:, 0: 1] - 1
        offset_y = self.img_size / 2 - idx_xy_dense_center[:, 1: 2] - 1
        idx_xy_dense_offset = idx_xy_dense + torch.cat([offset_x, offset_y], dim=1).unsqueeze(dim=1)

        # Expand Point Features
        f_dense = pc.unsqueeze(dim=2).expand(-1, -1, 25, -1).contiguous().view(pc.size(0), N * 25, 3)[..., 2: 3].repeat(1, 1, 3)
        
        idx_zero = idx_xy_dense_offset < 0
        idx_obj = idx_xy_dense_offset > 223
        idx_xy_dense_offset = idx_xy_dense_offset + idx_zero.to(torch.int32)
        idx_xy_dense_offset = idx_xy_dense_offset - idx_obj.to(torch.int32)

        # B N 9 C -> B 9N C
        assert idx_xy_dense_offset.min() >= 0 and idx_xy_dense_offset.max() <= (self.img_size-1), str(idx_xy_dense_offset.min()) + '-' + str(idx_xy_dense_offset.max())
        
        # change idx to 1-dim
        new_idx_xy_dense = idx_xy_dense_offset[:, :, 0] * self.img_size + idx_xy_dense_offset[:, :, 1]
        
        # Get Image Features
        out = scatter(f_dense, new_idx_xy_dense.long(), dim=1, reduce=self.proj_reduction) 

        # need to pad 
        if out.size(1) < self.img_size * self.img_size: 
            delta = self.img_size * self.img_size - out.size(1) 
            zero_pad = torch.zeros(out.size(0), delta, out.size(2)).to(out.device) 
            res = torch.cat([out, zero_pad], dim=1).reshape((out.size(0), self.img_size, self.img_size, out.size(2))) 
        else: 
            res = out.reshape((out.size(0), self.img_size, self.img_size, out.size(2))) 
        
        # B 224 224 C
        img = res.permute(0, 3, 1, 2).contiguous()
        mean_vec = self.img_mean.unsqueeze(dim=0).unsqueeze(dim=2).unsqueeze(dim=3).cuda()  # 1 3 1 1
        std_vec = self.img_std.unsqueeze(dim=0).unsqueeze(dim=2).unsqueeze(dim=3).cuda()   # 1 3 1 1
        # Normalize the pic        
        img = nn.Sigmoid()(img)
        img_norm = img.sub(mean_vec).div(std_vec)
        return img_norm, pc_min, grid_size, (offset_x, offset_y)

    def build_loss_func(self, loss_type):
        if loss_type == "cdl1":
            self.loss_func = ChamferDistanceL1().cuda()
        elif loss_type =='cdl2':
            self.loss_func = ChamferDistanceL2().cuda()
        else:
            raise NotImplementedError
            # self.loss_func = emd().cuda()

    def forward(self, pts, vis = False, **kwargs):
        neighborhood, center = self.group_divider(pts)

        '''Encoder'''
        x_vis, x_mask_without_pos, mask = self.MAE_encoder(neighborhood, center)
        B,_,C = x_vis.shape # B VIS C
        mask_pts = neighborhood[mask].reshape(B,-1,3)

        imgs_mask, pc_min_mask, grid_size_mask, offsets_mask = self.proj2img(mask_pts)  # b, 3, 224, 224
        with torch.no_grad():
    
            # ''' 2D Attention Maps '''
            # img_feats, img_sals = self.clip_model.encode_image(imgs_mask)  # b, c, h, w
            # img_sals = img_sals.float()[:, 0, 1:]  # b, 196
            # img_sals = img_sals.reshape(-1, 1, 14, 14)  # b, 1, 14, 14

            ''' 2D Representations Extraction '''
            img_feats = self.clip_model.encode_image(imgs_mask)  # b, c, h, w

            ''' 2D Visual Features '''
            img_feats = img_feats.float()


            
        pot_rec = self.pred_pos_proj(x_mask_without_pos) # B, Mask, C -> B, Mask, C
        tmp_img_feats = F.normalize(img_feats.detach(), dim=-1)
        tmp_pot_rec = F.normalize(pot_rec.mean(1), dim=-1)
        # loss2 = 1 - (tmp_img_feats * tmp_pot_rec).sum(dim=-1).mean()
        cos_sim = torch.sum(tmp_img_feats * tmp_pot_rec, dim=-1) / (torch.norm(tmp_img_feats, p=2, dim=-1) * 
                                                                    torch.norm(tmp_pot_rec, p=2, dim=-1))
        loss2 = 1 - cos_sim.mean()

        # if self.config.pred_pos_transformer_layer != 0:
        #     x_mask_without_pos = self.pred_pos_decoder(x_mask_without_pos)
        
        # pos_rec = self.pred_pos_proj(x_mask_without_pos) # B, Mask, C -> B, Mask, C
        # gt_pos = get_pos_embed(self.trans_dim, center[mask].reshape(B, -1, 3))
        # if self.pred_loss == 'l2':
        #     loss2 = F.mse_loss(pos_rec, gt_pos.detach())    
        # elif self.pred_loss == 'sml1':
        #     loss2 = torch.nn.SmoothL1Loss()(pos_rec, gt_pos.detach()).mean() 
        # elif self.pred_loss == 'cos':
        #     # B, Mask, C
        #     tmp_gt_pos = F.normalize(gt_pos.detach(), dim=-1)
        #     tmp_pos_rec = F.normalize(pos_rec, dim=-1)
        #     loss2 = 1 - (tmp_gt_pos * tmp_pos_rec).sum(dim=-1).mean()
        # elif self.pred_loss == 'l1':
        #     loss2 = F.l1_loss(pos_rec, gt_pos.detach())
        # else:
        #     raise NotImplementedError
        

        '''Self Points Decoder-Vis'''
        pos_emd_vis = self.MAE_encoder.pos_embed(get_pos_embed(self.trans_dim, center[~mask].reshape(B, -1, 3)))
        pos_emd_mask = self.MAE_encoder.pos_embed(get_pos_embed(self.trans_dim, center[mask].reshape(B, -1, 3)))
        
        # if self.add_detach:
            # pos_rec = self.MAE_encoder.pos_embed(pos_rec.detach())      
        # else:
            # pos_rec = self.MAE_encoder.pos_embed(pos_rec)   

        

        _,N,_ = pot_rec.shape
        mask_token = self.mask_token.expand(B, N, -1)
        x_full = torch.cat([x_vis, mask_token], dim=1)
        
        # pos_full = torch.cat([pos_emd_vis, pos_rec], dim=1)
        pos_full = torch.cat([pos_emd_vis, pos_emd_mask], dim=1)

        x_rec = self.MAE_decoder(x=x_full, pos=pos_full, return_token_num=N)

        B, M, C = x_rec.shape
        rebuild_points = self.increase_dim(x_rec.transpose(1, 2)).transpose(1, 2).reshape(B * M, -1, 3)  # B M 1024

        gt_points = neighborhood[mask].reshape(B * M,-1,3)
        loss1 = self.loss_func(rebuild_points, gt_points)



        '''Cross Center Decoder-Mask'''
        # cross_mask_full = pot_rec.detach()
        # x_cross_rec = self.MAE_Cross_decoder(x=x_full, y=cross_mask_full, pos=pos_full, return_token_num=N)
        # rebuild_cross_points = self.increase_cross_dim(x_cross_rec.transpose(1, 2)).transpose(1, 2).reshape(B * M, -1, 3)  # B M 1024

        # loss3 = self.loss_func(rebuild_cross_points, gt_points)




        
        if vis: #visualization
            vis_points = neighborhood[~mask].reshape(B * (self.num_group - M), -1, 3)
            full_vis = vis_points + center[~mask].unsqueeze(1)
            full_rebuild = rebuild_points + center[mask].unsqueeze(1)
            full = torch.cat([full_vis, full_rebuild], dim=0)
            # full_points = torch.cat([rebuild_points,vis_points], dim=0)
            full_center = torch.cat([center[mask], center[~mask]], dim=0)
            # full = full_points + full_center.unsqueeze(1)
            ret2 = full_vis.reshape(-1, 3).unsqueeze(0)
            ret1 = full.reshape(-1, 3).unsqueeze(0)
            # return ret1, ret2
            return ret1, ret2, full_center
        else:
            return loss1, self.config.ita * loss2
            # return loss1, self.config.ita * loss2, loss3

# finetune model
@MODELS.register_module()
class PointTransformer(nn.Module):
    def __init__(self, config, **kwargs):
        super().__init__()
        self.config = config

        self.trans_dim = config.trans_dim
        self.depth = config.depth
        self.drop_path_rate = config.drop_path_rate
        self.cls_dim = config.cls_dim
        self.num_heads = config.num_heads

        self.group_size = config.group_size
        self.num_group = config.num_group
        self.encoder_dims = config.encoder_dims

        self.group_divider = Group(num_group=self.num_group, group_size=self.group_size)

        self.encoder = Encoder(encoder_channel=self.encoder_dims)

        self.cls_token = nn.Parameter(torch.zeros(1, 1, self.trans_dim))
        self.cls_pos = nn.Parameter(torch.randn(1, 1, self.trans_dim))

        self.pos_embed = nn.Sequential(
            nn.Linear(self.trans_dim, self.trans_dim),
            nn.GELU(),
            nn.Linear(self.trans_dim, self.trans_dim)
        )

        dpr = [x.item() for x in torch.linspace(0, self.drop_path_rate, self.depth)]
        self.blocks = TransformerEncoder(
            embed_dim=self.trans_dim,
            depth=self.depth,
            drop_path_rate=dpr,
            num_heads=self.num_heads,
        )

        self.norm = nn.LayerNorm(self.trans_dim)

        if hasattr(config, 'type'):
            if config.type == "linear":
                self.cls_head_finetune = nn.Sequential(
                    nn.Linear(self.trans_dim * 2, self.cls_dim)
                )
                # raise ValueError
            else:
                self.cls_head_finetune = nn.Sequential(
                    nn.Linear(self.trans_dim * 2, 256),
                    nn.BatchNorm1d(256),
                    nn.ReLU(inplace=True),
                    nn.Dropout(0.5),
                    nn.Linear(256, 256),
                    nn.BatchNorm1d(256),
                    nn.ReLU(inplace=True),
                    nn.Dropout(0.5),
                    nn.Linear(256, self.cls_dim)
                )            
        else:    
            self.cls_head_finetune = nn.Sequential(
                nn.Linear(self.trans_dim * 2, 256),
                nn.BatchNorm1d(256),
                nn.ReLU(inplace=True),
                nn.Dropout(0.5),
                nn.Linear(256, 256),
                nn.BatchNorm1d(256),
                nn.ReLU(inplace=True),
                nn.Dropout(0.5),
                nn.Linear(256, self.cls_dim)
            )

        self.build_loss_func()

        trunc_normal_(self.cls_token, std=.02)
        trunc_normal_(self.cls_pos, std=.02)

    def build_loss_func(self):
        self.loss_ce = nn.CrossEntropyLoss()

    def get_loss_acc(self, ret, gt):
        loss = self.loss_ce(ret, gt.long())
        pred = ret.argmax(-1)
        acc = (pred == gt).sum() / float(gt.size(0))
        return loss, acc * 100

    def load_model_from_ckpt(self, bert_ckpt_path):
        if bert_ckpt_path is not None:
            ckpt = torch.load(bert_ckpt_path)
            base_ckpt = {k.replace("module.", ""): v for k, v in ckpt['base_model'].items()}

            for k in list(base_ckpt.keys()):
                if k.startswith('MAE_encoder') :
                    base_ckpt[k[len('MAE_encoder.'):]] = base_ckpt[k]
                    del base_ckpt[k]
                elif k.startswith('base_model'):
                    base_ckpt[k[len('base_model.'):]] = base_ckpt[k]
                    del base_ckpt[k]

            incompatible = self.load_state_dict(base_ckpt, strict=False)

            if incompatible.missing_keys:
                print_log('missing_keys', logger='Transformer')
                print_log(
                    get_missing_parameters_message(incompatible.missing_keys),
                    logger='Transformer'
                )
            if incompatible.unexpected_keys:
                print_log('unexpected_keys', logger='Transformer')
                print_log(
                    get_unexpected_parameters_message(incompatible.unexpected_keys),
                    logger='Transformer'
                )

            print_log(f'[Transformer] Successful Loading the ckpt from {bert_ckpt_path}', logger='Transformer')
        else:
            print_log('Training from scratch!!!', logger='Transformer')
            self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
        elif isinstance(m, nn.Conv1d):
            trunc_normal_(m.weight, std=.02)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def forward(self, pts):

        neighborhood, center = self.group_divider(pts)
        group_input_tokens = self.encoder(neighborhood)  # B G N

        cls_tokens = self.cls_token.expand(group_input_tokens.size(0), -1, -1)
        cls_pos = self.cls_pos.expand(group_input_tokens.size(0), -1, -1)

        pos = self.pos_embed(get_pos_embed(self.trans_dim, center))

        x = torch.cat((cls_tokens, group_input_tokens), dim=1)
        pos = torch.cat((cls_pos, pos), dim=1)
        # transformer
        x = self.blocks(x, pos)
        x = self.norm(x)
        concat_f = torch.cat([x[:, 0], x[:, 1:].max(1)[0]], dim=-1)
        ret = self.cls_head_finetune(concat_f)
        return ret
