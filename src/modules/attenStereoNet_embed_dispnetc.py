from __future__ import print_function
import torch
import torch.nn as nn
import torch.utils.data
from torch.autograd import Variable
import torch.nn.functional as F
import math


from .embednetwork import embed_net
from .bilateral import bilateralFilter
#from ..baselines.DispNet.models.dispnet import (
#        correlation1D_map_V1,
#        downsample_conv_bn,
#        conv3x3_bn,
#        upconv3x3_bn,
#        upconv4x4_bn,
#        conv1x1_bn)
#from src.net_init import net_init_v0
#from torch.nn.parameter import Parameter
from ..baselines.DispNet.models.dispnet import DispNet

"""
our network
"""
# adapted from DispNetC:
#class AttenStereoNet(nn.Module):
# updated: using Python Inheritance:
class AttenStereoNet(DispNet):
    def __init__(self, maxdisp=192, 
            sigma_s = 0.7, # 1.7: 13 x 13; 0.3 : 3 x 3;
            sigma_v = 0.1, 
            isEmbed = True, 
            dilation = 2,
            cost_filter_grad = True
            ):
        
        """ the following is from DispNetC """
        # due to TWO consecutive downsampling, so here maxdisp=40, 
        # actually means 4*maxdisp=160 in the original input image pair;
        super(AttenStereoNet, self).__init__( 
                is_corr = True, 
                maxdisp_corr = maxdisp //4, # for maxdisp_corr!! 
                corr_func_type = 'correlation1D_map_V1',
                is_bn = True,
                is_relu = True)
        
        self.isEmbed = isEmbed # True of False
        self.sigma_s = sigma_s
        self.sigma_v = sigma_v
        #TODO:
        #if not fixed sigma_v:
        #from torch.nn.parameter import Parameter
        #self.sigma_v = Parameter(sigma_v * torch.ones(out_channels))
        
        self.dilation = dilation
        self.cost_filter_grad = cost_filter_grad
        """ embedding network """
        if self.isEmbed:
            print(' Enable Embedding Network!!!')
            self.embednet = embed_net()
            #the module layer:
            self.bifilter = bilateralFilter(sigma_s, sigma_v, isCUDA = True, dilation = self.dilation)
        else:
            self.embednet = None
            self.bifilter = None

        """ the followind initilization is omitted due to inheritance from DispNet """ 
        #self.corr_func = correlation1D_map_V1(self.maxdisp_corr)


    def forward(self, x, y):
        """ DispNetC """
        #left image, in size [N, 3, H, W]
        out_x = self.conv1(x)
        shortcut_c1 = out_x
        out_x = self.conv2(out_x)
        shortcut_c2 = out_x
        
        #right image
        out_y = self.conv1(y)
        out_y = self.conv2(out_y)
        # correlation map, in [N, D, H/4, W/4]
        out = self.corr_func(out_x, out_y)
        out_redir = self.conv_redir(out_x)
        """ Try 1: adding our filtering module here """
        if not self.isEmbed:
            embed = None
        else:
            # downscale x to [N,C,H/4, W/4] then fed into embeddingnet,
            # because the cost volume generated below is in shape [N,C,D/4, H/4, W/4]
            left_scale = F.interpolate(x, [x.size()[2]//4, x.size()[3]//4], 
                    mode='bilinear', align_corners=True)
            #print ('[???] left shape', x.shape)
            #print ('[???] left_scale shape', left_scale.shape)
            """ embed shape [2, 64, 64, 128]"""
            embed = self.embednet(left_scale)
            #print ('[???] embed shape', embed.shape)

            """ correlation map in shape, [N, C=maxdisp/4, H/4, W/4]"""
            #print ('[???] correlation shape', out.shape)
            # NOTE: this might be the memory consuming!!!
            with torch.set_grad_enabled(self.cost_filter_grad):
                out = self.bifilter(embed, out.contiguous()).contiguous()
                #print ('[???] filtered correlation shape', out.shape)
                #NOTE: V3: disable this part;
                #out_redir = self.bifilter(embed, out_redir.contiguous())
                #out_redir = out_redir.contiguous()
                #print ('[???] filtered out_redir shape', out_redir.shape)

        out = self.conv3a(torch.cat((out,out_redir),dim=1))
        
        # commen parts
        out = self.conv3b(out)
        shortcut_c3 = out
        out = self.conv4a(out)
        out = self.conv4b(out)
        shortcut_c4 = out
        out = self.conv5a(out)
        out = self.conv5b(out)
        shortcut_c5 = out
        out = self.conv6a(out)
        out = self.conv6b(out)
        disp6 = self.conv_disp6(out) # in size [N, 1, H/64, W/64]
        
        # decoder 5
        out = self.upc5(out)
        out = self.ic5(torch.cat((out, shortcut_c5, self.upconv_disp6(disp6)), dim=1))
        disp5 = self.conv_disp5(out) # in size [N, 1, H/32, W/32]

        # decoder 4
        out = self.upc4(out)
        out = self.ic4(torch.cat((out, shortcut_c4, self.upconv_disp5(disp5)), dim=1))
        disp4 = self.conv_disp4(out) # in size [N, 1, H/16, W/16]
        
        # decoder 3
        out = self.upc3(out)
        out = self.ic3(torch.cat((out, shortcut_c3, self.upconv_disp4(disp4)), dim=1))
        disp3 = self.conv_disp3(out) # in size [N, 1, H/8, W/8]
        #print ("[???] disp3: ", disp3.shape)
        
        # decoder 2
        out = self.upc2(out)
        out = self.ic2(torch.cat((out, shortcut_c2, self.upconv_disp3(disp3)), dim=1))
        disp2 = self.conv_disp2(out) # in size [N, 1, H/4, W/4]
        #print ("[???] disp2: ", disp2.shape)
        
        # decoder 1
        out = self.upc1(out)
        out = self.ic1(torch.cat((out, shortcut_c1, self.upconv_disp2(disp2)), dim=1))
        disp1 = self.conv_disp1(out) # in size [N, 1, H/2, W/2]
        #print ("[???] disp1: ", disp1.shape)
        
        # added by CCJ for original size disp as output
        # NOTE: disp3 is in 1/8 size ==> interpolated to full size;
        H3, W3 = disp3.size()[2:]
        H0, W0 = 8*H3, 8*W3
        disp_out1 = torch.squeeze(
                F.interpolate(disp3, [H0, W0], mode='bilinear', align_corners = True),
                1)# squeeze disp [N, 1, H, W] to [N, H, W]
        
        # NOTE: disp2 is in 1/4 size ==> interpolated to full size;
        disp_out2 = torch.squeeze(
                F.interpolate(disp2, [H0, W0], mode='bilinear', align_corners = True),
                1)# squeeze disp [N, 1, H, W] to [N, H, W]
        
        # NOTE: disp1 is in 1/2 size ==> interpolated to full size;
        disp_out3 = torch.squeeze(
                F.interpolate(disp1, [H0, W0], mode='bilinear', align_corners = True),
                1)# squeeze disp [N, 1, H, W] to [N, H, W]
        
        if self.training:
            return disp_out1, disp_out2, disp_out3, embed
        else:
            return disp_out3, embed