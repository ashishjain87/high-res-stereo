import argparse
import logging
import cv2
import math
import matplotlib.pyplot as plt
from models import hsm
import numpy as np
import os
import pdb
import skimage.io
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
import time
from models.submodule import *
from utils.eval import mkdir_p, save_pfm
from utils.preprocess import get_transform
#cudnn.benchmark = True
cudnn.benchmark = False
    
def generate_disparity_label(test_left_img, test_right_img, model, outdir, max_disp=-1, testres=1):
    logger = logging.getLogger(__name__)

    processed = get_transform()
    model.eval()
    disp_maps = []
    for inx in range(len(test_left_img)):
        logger.info('Processing %s/%s' % (inx, len(test_left_img)))
        imgL_o = (skimage.io.imread(test_left_img[inx]).astype('float32'))[:,:,:3]
        imgR_o = (skimage.io.imread(test_right_img[inx]).astype('float32'))[:,:,:3]
        imgsize = imgL_o.shape[:2]

        if max_disp>0:
            if max_disp % 16 != 0:
                max_disp = 16 * math.floor(max_disp/16)
            max_disp = int(max_disp)
        else:
            max_disp = 384 # manually set max_disp for KITTI

        ## change max disp
        tmpdisp = int(max_disp*testres//64*64)
        if (max_disp*testres/64*64) > tmpdisp:
            model.module.maxdisp = tmpdisp + 64
        else:
            model.module.maxdisp = tmpdisp
        if model.module.maxdisp ==64: model.module.maxdisp=128
        model.module.disp_reg8 =  disparityregression(model.module.maxdisp,16).cuda()
        model.module.disp_reg16 = disparityregression(model.module.maxdisp,16).cuda()
        model.module.disp_reg32 = disparityregression(model.module.maxdisp,32).cuda()
        model.module.disp_reg64 = disparityregression(model.module.maxdisp,64).cuda()
        logger.debug("Maximum disparity of model is set to %d" % model.module.maxdisp)
        
        # resize
        imgL_o = cv2.resize(imgL_o,None,fx=testres,fy=testres,interpolation=cv2.INTER_CUBIC)
        imgR_o = cv2.resize(imgR_o,None,fx=testres,fy=testres,interpolation=cv2.INTER_CUBIC)
        imgL = processed(imgL_o).numpy()
        imgR = processed(imgR_o).numpy()

        imgL = np.reshape(imgL,[1,3,imgL.shape[1],imgL.shape[2]])
        imgR = np.reshape(imgR,[1,3,imgR.shape[1],imgR.shape[2]])

        ##fast pad
        max_h = int(imgL.shape[2] // 64 * 64)
        max_w = int(imgL.shape[3] // 64 * 64)
        if max_h < imgL.shape[2]: max_h += 64
        if max_w < imgL.shape[3]: max_w += 64

        top_pad = max_h-imgL.shape[2]
        left_pad = max_w-imgL.shape[3]
        imgL = np.lib.pad(imgL,((0,0),(0,0),(top_pad,0),(0,left_pad)),mode='constant',constant_values=0)
        imgR = np.lib.pad(imgR,((0,0),(0,0),(top_pad,0),(0,left_pad)),mode='constant',constant_values=0)

        # test
        imgL = Variable(torch.FloatTensor(imgL).cuda())
        imgR = Variable(torch.FloatTensor(imgR).cuda())
        with torch.no_grad():
            torch.cuda.synchronize()
            start_time = time.time()
            pred_disp,entropy = model(imgL,imgR)
            torch.cuda.synchronize()
            ttime = (time.time() - start_time)
            logger.debug('time = %.2f' % (ttime*1000) )
        pred_disp = torch.squeeze(pred_disp).data.cpu().numpy()

        top_pad   = max_h-imgL_o.shape[0]
        left_pad  = max_w-imgL_o.shape[1]
        entropy = entropy[top_pad:,:pred_disp.shape[1]-left_pad].cpu().numpy()
        pred_disp = pred_disp[top_pad:,:pred_disp.shape[1]-left_pad]

        # save predictions
        idxname = os.path.split(test_left_img[inx])[1].split('.')[0]
        if not os.path.exists('%s'%(outdir)):
            os.makedirs('%s'%(outdir))

        # resize to highres
        pred_disp = cv2.resize(pred_disp/testres,(imgsize[1],imgsize[0]),interpolation=cv2.INTER_LINEAR)

        # clip while keep inf
        invalid = np.logical_or(pred_disp == np.inf,pred_disp!=pred_disp)
        pred_disp[invalid] = np.inf
        
        disp_maps.append([idxname, pred_disp])
        
        # generating colormap
        #plt.imshow(pred_disp/pred_disp[~invalid].max()*255, cmap='plasma')
        #plt.savefig('color_disparity', dpi=800)
        
        logger.info('Saving disparity map %s.npy at %s' % (idxname, outdir))
        np.save('%s/%s.npy'% (outdir, idxname),(pred_disp))
        # cv2.imwrite('%s/%s.png'% (outdir, idxname),pred_disp/pred_disp[~invalid].max()*255)
            
        torch.cuda.empty_cache()
    
    return disp_maps

def preprocess(left_datapath, right_datapath, loadmodel, clean=1.0, level=1):
    logger = logging.getLogger(__name__)
    # setting data path
    logger.info('Processing left image data path %s' % (left_datapath))
    test_left_img = [os.path.join(left_datapath, file) for file in os.listdir(left_datapath)]
    logger.info('Processing right image data path %s' % (right_datapath))
    test_right_img = [os.path.join(right_datapath, file) for file in os.listdir(right_datapath)]

    # construct model
    logger.info('Loading model')
    model = hsm(128, clean, level)
    model = nn.DataParallel(model, device_ids=[0])
    model.cuda()

    if loadmodel is not None:
        pretrained_dict = torch.load(loadmodel)
        pretrained_dict['state_dict'] =  {k:v for k,v in pretrained_dict['state_dict'].items() if 'disp' not in k}
        model.load_state_dict(pretrained_dict['state_dict'],strict=False)
    else:
        logger.debug('run with random init')
    logger.debug('Number of model parameters: {}'.format(sum([p.data.nelement() for p in model.parameters()])))
    
    return test_left_img, test_right_img, model

def main():
    parser = argparse.ArgumentParser(description='HSM')
    parser.add_argument('--datapath', default='./data-mbtest/', help='test data path')
    parser.add_argument('--loadmodel', default=None, help='model path')
    parser.add_argument('--outdir', default='output', help='output dir')
    parser.add_argument('--clean', type=float, default=-1, help='clean up output using entropy estimation')
    parser.add_argument('--testres', type=float, default=0.5, help='test time resolution ratio 0-x')
    parser.add_argument('--max_disp', type=float, default=-1, help='maximum disparity to search for')
    parser.add_argument('--level', type=int, default=1, help='output level of output, default is level 1 (stage 3),\
                          can also use level 2 (stage 2) or level 3 (stage 1)')
    args = parser.parse_args()
    
    test_left_img, test_right_img, model = preprocess(args.datapath, args.loadmodel, args.clean, args.level)
    generate_disparity_label(test_left_img, test_right_img, model, args.outdir, args.max_disp, args.testres)

if __name__ == '__main__':
    main()