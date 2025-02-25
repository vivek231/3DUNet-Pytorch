from utils.common import *
from scipy import ndimage
import numpy as np
from torchvision import transforms as T
import torch, os
from torch.utils.data import Dataset, DataLoader
from glob import glob
import math
import SimpleITK as sitk

class Img_DataSet(Dataset):
    def __init__(self, data_path, label_path, cut, resize_scale=1):
        self.resize_scale = resize_scale
        self.label_path = label_path
        self.data_path = data_path
        self.n_labels = 3
        # 读取一个data文件并归一化 shape:[s,h,w]
        self.data_np = sitk_read_raw(self.data_path)
        if self.resize_scale != 1:
            self.data_np = ndimage.zoom(self.data_np, zoom=self.resize_scale, order=3) # 双三次重采样
        self.data_np = norm_img(self.data_np)
        self.ori_shape = self.data_np.shape
        # 读取一个label文件 shape:[s,h,w]
        self.label_np = sitk_read_raw(self.label_path)
        if self.resize_scale != 1:
            self.label_np = ndimage.zoom(self.label_np, zoom=self.resize_scale, order=0) # 最近邻重采样
        # 扩展一定数量的slices，以保证卷积下采样合理运算
        self.cut = cut

        self.data_np = self.padding_img(self.data_np, self.cut)
        self.new_shape = self.data_np.shape
        self.data_np = self.extract_ordered_overlap(self.data_np, self.cut)

    def __getitem__(self, index):
        data = self.data_np[index]
        # target = self.label_np[index]
        return torch.from_numpy(data)

    def __len__(self):
        return len(self.data_np)

    def padding_img(self, img, C):
        assert (len(img.shape) == 3)  # 3D array
        img_s, img_h, img_w = img.shape
        leftover_s = (img_s - C['patch_s']) % C['stride_s']
        leftover_h = (img_h - C['patch_h']) % C['stride_h']
        leftover_w = (img_w - C['patch_w']) % C['stride_w']
        if (leftover_s != 0):
            s = img_s + (C['stride_s'] - leftover_s)
        else:
            s = img_s

        if (leftover_h != 0):
            h = img_h + (C['stride_h'] - leftover_h)
        else:
            h = img_h

        if (leftover_w != 0):
            w = img_w + (C['stride_w'] - leftover_w)
        else:
            w = img_w

        tmp_full_imgs = np.zeros((s, h, w))
        tmp_full_imgs[:img_s, :img_h, 0:img_w] = img
        print("Padded images shape: " + str(img.shape))
        return tmp_full_imgs

    # Divide all the full_imgs in pacthes
    def extract_ordered_overlap(self, img, C):
        assert (len(img.shape) == 3)  # 3D arrays
        img_s, img_h, img_w = img.shape
        assert ((img_h - C['patch_h']) % C['stride_h'] == 0
                and (img_w - C['patch_w']) % C['stride_w'] == 0
                and (img_s - C['patch_s']) % C['stride_s'] == 0)
        N_patches_s = (img_s - C['patch_s']) // C['stride_s'] + 1
        N_patches_h = (img_h - C['patch_h']) // C['stride_h'] + 1
        N_patches_w = (img_w - C['patch_w']) // C['stride_w'] + 1
        N_patches_img = N_patches_s * N_patches_h * N_patches_w
        print("Patches number of the image:{} [s={} | h={} | w={}]"\
                .format(N_patches_img, N_patches_s, N_patches_h, N_patches_w))
        patches = np.empty((N_patches_img, C['patch_s'], C['patch_h'], C['patch_w']))
        iter_tot = 0  # iter over the total number of patches (N_patches)
        for s in range(N_patches_s):  # loop over the full images
            for h in range(N_patches_h):
                for w in range(N_patches_w):
                    patch = img[s * C['stride_s'] : s * C['stride_s']+C['patch_s'],
                            h * C['stride_h']: h * C['stride_h']+C['patch_h'],
                            w * C['stride_w']: w * C['stride_w']+C['patch_w']]
                    patches[iter_tot] = patch
                    iter_tot += 1  # total
        assert (iter_tot == N_patches_img)
        return patches  # array with all the full_imgs divided in patches


class Recompone_tool():
    def __init__(self, img_ori_shape, img_new_shape, C):
        self.result = None
        self.ori_shape = img_ori_shape
        self.new_shape = img_new_shape
        self.C = C

    def add_result(self, tensor):
        # tensor = tensor.detach().cpu() # shape: [N,class,s,h,w]
        # tensor_np = np.squeeze(tensor_np,axis=0)
        if self.result is not None:
            self.result = torch.cat((self.result, tensor), dim=0)
        else:
            self.result = tensor

    def recompone_overlap(self):
        """
        :param preds: output of model  shape：[N_patchs_img,3,patch_s,patch_h,patch_w]
        :return: result of recompone output shape: [3,img_s,img_h,img_w]
        """
        patch_s = self.result.shape[2]
        patch_h = self.result.shape[3]
        patch_w = self.result.shape[4]
        N_patches_s = (self.new_shape[0] - patch_s) // self.C['stride_s'] + 1
        N_patches_h = (self.new_shape[1] - patch_h) // self.C['stride_h'] + 1
        N_patches_w = (self.new_shape[2] - patch_w) // self.C['stride_w'] + 1
        N_patches_img = N_patches_s * N_patches_h * N_patches_w
        # print("N_patches_s/h/w:", N_patches_s, N_patches_h, N_patches_w)
        # print("N_patches_img: " + str(N_patches_img))
        assert (self.result.shape[0] == N_patches_img)

        full_prob = torch.zeros((3, self.new_shape[0], self.new_shape[1],
                              self.new_shape[2]))  # itialize to zero mega array with sum of Probabilities
        full_sum = torch.zeros((3, self.new_shape[0], self.new_shape[1], self.new_shape[2]))
        k = 0  # iterator over all the patches
        for s in range(N_patches_s):
            for h in range(N_patches_h):
                for w in range(N_patches_w):
                    full_prob[:, s * self.C['stride_s']:s * self.C['stride_s'] + patch_s,
                                 h * self.C['stride_h']:h  * self.C['stride_h'] + patch_h,
                                 w * self.C['stride_w']:w * self.C['stride_w'] + patch_w] += self.result[k]
                    full_sum[:, s * self.C['stride_s']:s * self.C['stride_s'] + patch_s,
                                h * self.C['stride_h']:h * self.C['stride_h'] + patch_h,
                                w * self.C['stride_w']:w * self.C['stride_w'] + patch_w] += 1
                    k += 1
        assert (k == self.result.size(0))
        assert (torch.min(full_sum) >= 1.0)  # at least one
        final_avg = full_prob / full_sum
        # print(final_avg.size())
        assert (torch.max(final_avg) <= 1.0)  # max value for a pixel is 1.0
        assert (torch.min(final_avg) >= 0.0)  # min value for a pixel is 0.0
        img = final_avg[:, :self.ori_shape[0], :self.ori_shape[1], :self.ori_shape[2]]
        return img


def Test_Datasets(dataset_path, cut_param, resize_scale=1):
    data_list = sorted(glob(os.path.join(dataset_path, 'data/*')))
    label_list = sorted(glob(os.path.join(dataset_path, 'label/*')))
    print("The number of test samples is: ", len(data_list))
    for datapath, labelpath in zip(data_list, label_list):
        print("\nStart Evaluate: ", datapath)
        yield Img_DataSet(datapath, labelpath, cut_param,resize_scale=resize_scale), datapath.split('-')[-1]

# 测试代码
import matplotlib.pyplot as plt
from tqdm import tqdm
if __name__ == '__main__':
    test_path = '/ssd/lzq/dataset/LiTS/test'
    cut_param = {'patch_s': 32,
                 'patch_h': 128,
                 'patch_w': 128,
                 'stride_s': 24,
                 'stride_h': 96,
                 'stride_w': 96}
    for dataset,file_idx in Test_Datasets(test_path, cut_param):
        data_loader = DataLoader(dataset=dataset, batch_size=4, num_workers=0, shuffle=False)
        print(len(data_loader))
        with torch.no_grad():
            for data in data_loader:
                data = data.unsqueeze(1)
                print(data.shape)
                plt.imshow(data[0, 0, 0])
                plt.show()