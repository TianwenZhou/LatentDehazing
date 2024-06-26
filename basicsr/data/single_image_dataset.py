from os import path as osp
from torch.utils import data as data
from torchvision.transforms.functional import normalize

from basicsr.data.data_util import paths_from_lmdb, paths_from_folder
from basicsr.utils import FileClient, imfrombytes, img2tensor, rgb2ycbcr, scandir
from basicsr.utils.registry import DATASET_REGISTRY
from ldm.util import instantiate_from_config
from omegaconf import OmegaConf
from basicsr.data.FDA import trans_image_by_ref
from basicsr.data.transforms import augment
import torchvision.utils as vutils
from einops import rearrange, repeat
from contextlib import nullcontext
from torch import autocast


from pathlib import Path
import random
import cv2
import numpy as np
import copy
import torch
from PIL import Image

@DATASET_REGISTRY.register()
class SingleImageDataset(data.Dataset):
    """Read only lq images in the test phase.

    Read LQ (Low Quality, e.g. LR (Low Resolution), blurry, noisy, etc).

    There are two modes:
    1. 'meta_info_file': Use meta information file to generate paths.
    2. 'folder': Scan folders to generate paths.

    Args:
        opt (dict): Config for train datasets. It contains the following keys:
            dataroot_lq (str): Data root path for lq.
            meta_info_file (str): Path for meta information file.
            io_backend (dict): IO backend type and other kwarg.
    """

    def __init__(self, opt):
        super(SingleImageDataset, self).__init__()
        self.opt = opt
        # file client (io backend)
        self.file_client = None
        self.io_backend_opt = opt['io_backend']
        self.mean = opt['mean'] if 'mean' in opt else None
        self.std = opt['std'] if 'std' in opt else None
        self.lq_folder = opt['dataroot_lq']

        if self.io_backend_opt['type'] == 'lmdb':
            self.io_backend_opt['db_paths'] = [self.lq_folder]
            self.io_backend_opt['client_keys'] = ['lq']
            self.paths = paths_from_lmdb(self.lq_folder)
        elif 'meta_info_file' in self.opt:
            with open(self.opt['meta_info_file'], 'r') as fin:
                self.paths = [osp.join(self.lq_folder, line.rstrip().split(' ')[0]) for line in fin]
        else:
            self.paths = sorted(list(scandir(self.lq_folder, full_path=True)))

    def __getitem__(self, index):
        if self.file_client is None:
            self.file_client = FileClient(self.io_backend_opt.pop('type'), **self.io_backend_opt)

        # load lq image
        lq_path = self.paths[index]
        img_bytes = self.file_client.get(lq_path, 'lq')
        img_lq = imfrombytes(img_bytes, float32=True)

        # color space transform
        if 'color' in self.opt and self.opt['color'] == 'y':
            img_lq = rgb2ycbcr(img_lq, y_only=True)[..., None]

        # BGR to RGB, HWC to CHW, numpy to tensor
        img_lq = img2tensor(img_lq, bgr2rgb=True, float32=True)
        # normalize
        if self.mean is not None or self.std is not None:
            normalize(img_lq, self.mean, self.std, inplace=True)
        return {'lq': img_lq, 'lq_path': lq_path}

    def __len__(self):
        return len(self.paths)

@DATASET_REGISTRY.register()
class SingleImageNPDatasetOnline(data.Dataset):
    """Read only lq images in the test phase.

    Read diffusion generated data for training CFW.

    Args:
        opt (dict): Config for train datasets. It contains the following keys:
            gt_path: Data root path for training data. The path needs to contain the following folders:
                gts: Ground-truth images.
                inputs: Input LQ images.
                latents: The corresponding HQ latent code generated by diffusion model given the input LQ image.
                samples: The corresponding HQ image given the HQ latent code, just for verification.
            io_backend (dict): IO backend type and other kwarg.
    """

    def __init__(self, opt):
        super(SingleImageNPDatasetOnline, self).__init__()
        self.opt = opt
        # file client (io backend)
        self.file_client = None
        self.ckpt_path = opt['ckpt_path'] if 'ckpt_path' in opt else None
        self.io_backend_opt = opt['io_backend']
        self.mean = opt['mean'] if 'mean' in opt else None
        self.std = opt['std'] if 'std' in opt else None
        self.crop_size = 512
        self.batch_size = 4
        self.ref_imgs = paths_from_folder(opt['ref_path'])
        if 'image_type' not in opt:
            opt['image_type'] = 'png'
        config = OmegaConf.load(opt["config"])
        self.device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        self.model = load_model_from_config(config, opt['ckpt_path'])
        self.model = self.model.to(self.device)

        self.model.register_schedule(given_betas=None, beta_schedule="linear", timesteps=1000,
						  linear_start=0.00085, linear_end=0.0120, cosine_s=8e-3)
        self.model.num_timesteps = 1000

        use_timesteps = set(space_timesteps(1000, [1000]))
        last_alpha_cumprod = 1.0
        new_betas = []
        timestep_map = []
        for i, alpha_cumprod in enumerate(self.model.alphas_cumprod):
            if i in use_timesteps:
                new_betas.append(1 - alpha_cumprod / last_alpha_cumprod)
                last_alpha_cumprod = alpha_cumprod
                timestep_map.append(i)
        new_betas = [beta.data.cpu().numpy() for beta in new_betas]
        self.model.register_schedule(given_betas=np.array(new_betas), timesteps=len(new_betas))
        self.model.num_timesteps = 1000
        self.model_ori = copy.deepcopy(self.model)
        self.model_ori = self.model_ori.to(self.device)

        self.model.ori_timesteps = list(use_timesteps)
        self.model.ori_timesteps.sort()
        self.model = self.model.to(self.device)

        if isinstance(opt['gt_path'], str):
            self.gt_paths = sorted([str(x) for x in Path(opt['gt_path']).glob('*.'+opt['image_type'])])
            self.depth_paths = sorted([str(x) for x in Path(opt['depth_path']).glob('*.'+opt['image_type'])])
            
        else:
            self.gt_paths = sorted([str(x) for x in Path(opt['gt_path'][0]).glob('*.'+opt['image_type'])])
            self.depth_paths = sorted([str(x) for x in Path(opt['depth_path'][0]).glob('*.'+opt['image_type'])])
           
            if len(opt['gt_path']) > 1:
                for i in range(len(opt['gt_path'])-1):
                    self.gt_paths.extend(sorted([str(x) for x in Path(opt['gt_path'][i+1]).glob('*.'+opt['image_type'])]))
                    self.depth_paths.extend(sorted([str(x) for x in Path(opt['depth_path'][i+1]).glob('*.'+opt['image_type'])]))
        



    def __getitem__(self, index):
        if self.file_client is None:
            self.file_client = FileClient(self.io_backend_opt.pop('type'), **self.io_backend_opt)

        # load gt image
        gt_path = self.gt_paths[index]
        img_bytes_gt = self.file_client.get(gt_path, 'gt')
        img_gt = imfrombytes(img_bytes_gt, float32=True)


        # load depth image
        depth_path = self.depth_paths[index]
        img_bytes_depth = self.file_client.get(depth_path, 'depth')
        img_depth = imfrombytes(img_bytes_depth,float32=True)


        # generate hazy image

        color_strategy = np.random.rand()
        if color_strategy <= 0.5: # 50%
            strategy = 'add_haze'
    #     elif 0.3 < color_strategy <= 0.6:
    #         strategy = 'luminance'
        else:
            strategy = 'colour_cast'


        # add_haze
        A = np.random.rand() * 1.3 + 0.5 #Generate A
        beta = 2 * np.random.rand() + 0.8 # Generate Beta
        # depth is a grayscale image


        img_f = img_gt

        td_bk = np.exp(- np.array(1 - img_depth) * beta)
        # td_bk = np.expand_dims(td_bk, axis=-1).repeat(3, axis=-1)
        img_bk = np.array(img_f) * td_bk + A * (1 - td_bk) # I_aug

        img_bk = img_bk / np.max(img_bk) * 255
        
        
        img_bk = img_bk[:, :, ::-1]

        if strategy == 'colour_cast':
                  # .covert('RGB')
            img_bk = Image.fromarray(np.uint8(img_bk))
            ref_num = random.randint(0, len(self.ref_imgs)-1)
            
            img_bk = trans_image_by_ref(
                    in_path=img_bk,
                    ref_path=self.ref_imgs[ref_num],
                    value=np.random.rand() * 0.002 + 0.0001
                )

        else:
            img_bk = img_bk / 255 # .covert('RGB')
        # img_bk = cv2.cvtColor((255 * img_bk).astype('uint8'), cv2.COLOR_BGR2RGB)
        # img_bk = img_bk / 255
        img_gt = cv2.cvtColor((255 * img_gt).astype('uint8'), cv2.COLOR_BGR2RGB)
        img_gt = img_gt / 255
  

        h, w, c = img_gt.shape[0:3]
        
        crop_pad_size = self.crop_size
        # pad
        if h != crop_pad_size or w != crop_pad_size:
            img_gt = cv2.resize(img_gt, (crop_pad_size,crop_pad_size))
            img_bk = cv2.resize(img_bk, (crop_pad_size,crop_pad_size))
           
        img_gt, img_bk = augment([img_gt,img_bk], self.opt['use_hflip'], self.opt['use_rot'])
        # BGR to RGB, HWC to CHW, numpy to tensor
        



        # color space transform
        if 'color' in self.opt and self.opt['color'] == 'y':
            img_bk = rgb2ycbcr(img_bk, y_only=True)[..., None]
            img_gt = rgb2ycbcr(img_gt, y_only=True)[..., None]
            
        img_bk = torch.from_numpy(img_bk[None].transpose(0,3,1,2)).to(self.device,dtype=torch.float)
        img_gt = torch.from_numpy(img_gt[None].transpose(0,3,1,2)).to(self.device,dtype=torch.float)
        precision_scope = nullcontext
        with torch.no_grad():
            with precision_scope("cuda"):
                with self.model.ema_scope():
                    init_latent = self.model.encode_first_stage(img_bk)
                    
                    init_latent = self.model.get_first_stage_encoding(init_latent)
                    
                    text_init = ['']*img_bk.size(0)
                    semantic_c = self.model.cond_stage_model(text_init)
                    noise = torch.randn_like(init_latent)
                    # t = repeat(torch.tensor([999]), '1 -> b', b=img_bk.size(0))
                    # t = t.to(self.device).long()
                    # x_T = self.model_ori.q_sample(x_start=init_latent, t=t, noise=noise)
                    x_T = noise
                    
                    latent, _ = self.model.sample(cond=semantic_c, struct_cond=init_latent, batch_size=img_bk.size(0), timesteps=1000, time_replace=1000, x_T=x_T, return_intermediates=True)
                    latent = latent.to(img_gt.device)
                    img_sample = self.model.decode_first_stage(latent)
                    img_sample = torch.clamp((img_sample+1.0)/2,min=0.0,max=1.0)

                    # BGR to RGB, HWC to CHW, numpy to tensor
                    
                    # latent = torch.from_numpy(latent.float())
                    
                    # img_gt = img2tensor([img_gt], bgr2rgb=True, float32=True)[0]
                    # img_bk = img2tensor([img_bk], bgr2rgb=False, float32=True)[0]
                    # img_depth = img2tensor([img_depth], bgr2rgb=True, float32=True)[0]
                    # img_sample = img2tensor([img_sample], bgr2rgb=True, float32=True)[0]
                    # normalize
                    if self.mean is not None or self.std is not None:
                        normalize(img_bk, self.mean, self.std, inplace=True)
                        normalize(img_gt, self.mean, self.std, inplace=True)
                        normalize(img_sample, self.mean, self.std, inplace=True)
        return {'hazy': img_bk, 'gt': img_gt, 'latent': latent[0], 'sample': img_sample}

    def __len__(self):
        return len(self.gt_paths)

def load_model_from_config(config, ckpt, verbose=False):
	print(f"Loading model from {ckpt}")
	pl_sd = torch.load(ckpt, map_location="cpu")
	if "global_step" in pl_sd:
		print(f"Global Step: {pl_sd['global_step']}")
	sd = pl_sd["state_dict"]
	model = instantiate_from_config(config.model)
	m, u = model.load_state_dict(sd, strict=False)
	if len(m) > 0 and verbose:
		print("missing keys:")
		print(m)
	if len(u) > 0 and verbose:
		print("unexpected keys:")
		print(u)

	model.cuda()
	model.eval()
	return model

def space_timesteps(num_timesteps, section_counts):
	"""
	Create a list of timesteps to use from an original diffusion process,
	given the number of timesteps we want to take from equally-sized portions
	of the original process.
	For example, if there's 300 timesteps and the section counts are [10,15,20]
	then the first 100 timesteps are strided to be 10 timesteps, the second 100
	are strided to be 15 timesteps, and the final 100 are strided to be 20.
	If the stride is a string starting with "ddim", then the fixed striding
	from the DDIM paper is used, and only one section is allowed.
	:param num_timesteps: the number of diffusion steps in the original
						  process to divide up.
	:param section_counts: either a list of numbers, or a string containing
						   comma-separated numbers, indicating the step count
						   per section. As a special case, use "ddimN" where N
						   is a number of steps to use the striding from the
						   DDIM paper.
	:return: a set of diffusion steps from the original process to use.
	"""
	if isinstance(section_counts, str):
		if section_counts.startswith("ddim"):
			desired_count = int(section_counts[len("ddim"):])
			for i in range(1, num_timesteps):
				if len(range(0, num_timesteps, i)) == desired_count:
					return set(range(0, num_timesteps, i))
			raise ValueError(
				f"cannot create exactly {num_timesteps} steps with an integer stride"
			)
		section_counts = [int(x) for x in section_counts.split(",")]   #[250,]
	size_per = num_timesteps // len(section_counts)
	extra = num_timesteps % len(section_counts)
	start_idx = 0
	all_steps = []
	for i, section_count in enumerate(section_counts):
		size = size_per + (1 if i < extra else 0)
		if size < section_count:
			raise ValueError(
				f"cannot divide section of {size} steps into {section_count}"
			)
		if section_count <= 1:
			frac_stride = 1
		else:
			frac_stride = (size - 1) / (section_count - 1)
		cur_idx = 0.0
		taken_steps = []
		for _ in range(section_count):
			taken_steps.append(start_idx + round(cur_idx))
			cur_idx += frac_stride
		all_steps += taken_steps
		start_idx += size
	return set(all_steps)


@DATASET_REGISTRY.register()
class SingleImageNPDataset(data.Dataset):
    """Read only lq images in the test phase.

    Read diffusion generated data for training CFW.

    Args:
        opt (dict): Config for train datasets. It contains the following keys:
            gt_path: Data root path for training data. The path needs to contain the following folders:
                gts: Ground-truth images.
                inputs: Input LQ images.
                latents: The corresponding HQ latent code generated by diffusion model given the input LQ image.
                samples: The corresponding HQ image given the HQ latent code, just for verification.
            io_backend (dict): IO backend type and other kwarg.
    """

    def __init__(self, opt):
        super(SingleImageNPDataset, self).__init__()
        self.opt = opt
        # file client (io backend)
        self.file_client = None
        self.io_backend_opt = opt['io_backend']
        self.mean = opt['mean'] if 'mean' in opt else None
        self.std = opt['std'] if 'std' in opt else None
        if 'image_type' not in opt:
            opt['image_type'] = 'png'

        if isinstance(opt['gt_path'], str):
            self.gt_paths = sorted([str(x) for x in Path(opt['gt_path']+'/gts').glob('*.'+opt['image_type'])])
            self.lq_paths = sorted([str(x) for x in Path(opt['gt_path']+'/inputs').glob('*.'+opt['image_type'])])
            self.np_paths = sorted([str(x) for x in Path(opt['gt_path']+'/latents').glob('*.npy')])
            self.sample_paths = sorted([str(x) for x in Path(opt['gt_path']+'/samples').glob('*.'+opt['image_type'])])
        else:
            self.gt_paths = sorted([str(x) for x in Path(opt['gt_path'][0]+'/gts').glob('*.'+opt['image_type'])])
            self.lq_paths = sorted([str(x) for x in Path(opt['gt_path'][0]+'/inputs').glob('*.'+opt['image_type'])])
            self.np_paths = sorted([str(x) for x in Path(opt['gt_path'][0]+'/latents').glob('*.npy')])
            self.sample_paths = sorted([str(x) for x in Path(opt['gt_path'][0]+'/samples').glob('*.'+opt['image_type'])])
            if len(opt['gt_path']) > 1:
                for i in range(len(opt['gt_path'])-1):
                    self.gt_paths.extend(sorted([str(x) for x in Path(opt['gt_path'][i+1]+'/gts').glob('*.'+opt['image_type'])]))
                    self.lq_paths.extend(sorted([str(x) for x in Path(opt['gt_path'][i+1]+'/inputs').glob('*.'+opt['image_type'])]))
                    self.np_paths.extend(sorted([str(x) for x in Path(opt['gt_path'][i+1]+'/latents').glob('*.npy')]))
                    self.sample_paths.extend(sorted([str(x) for x in Path(opt['gt_path'][i+1]+'/samples').glob('*.'+opt['image_type'])]))

        assert len(self.gt_paths) == len(self.lq_paths)
        assert len(self.gt_paths) == len(self.np_paths)
        assert len(self.gt_paths) == len(self.sample_paths)

    def __getitem__(self, index):
        if self.file_client is None:
            self.file_client = FileClient(self.io_backend_opt.pop('type'), **self.io_backend_opt)

        # load lq image
        lq_path = self.lq_paths[index]
        gt_path = self.gt_paths[index]
        sample_path = self.sample_paths[index]
        np_path = self.np_paths[index]

        img_bytes = self.file_client.get(lq_path, 'lq')
        img_lq = imfrombytes(img_bytes, float32=True)

        img_bytes_gt = self.file_client.get(gt_path, 'gt')
        img_gt = imfrombytes(img_bytes_gt, float32=True)

        img_bytes_sample = self.file_client.get(sample_path, 'sample')
        img_sample = imfrombytes(img_bytes_sample, float32=True)

        latent_np = np.load(np_path)

        # color space transform
        if 'color' in self.opt and self.opt['color'] == 'y':
            img_lq = rgb2ycbcr(img_lq, y_only=True)[..., None]
            img_gt = rgb2ycbcr(img_gt, y_only=True)[..., None]
            img_sample = rgb2ycbcr(img_sample, y_only=True)[..., None]

        # BGR to RGB, HWC to CHW, numpy to tensor
        img_lq = img2tensor(img_lq, bgr2rgb=True, float32=True)
        img_gt = img2tensor(img_gt, bgr2rgb=True, float32=True)
        img_sample = img2tensor(img_sample, bgr2rgb=True, float32=True)
        latent_np = torch.from_numpy(latent_np).float()
        latent_np = latent_np.to(img_gt.device)
        # normalize
        if self.mean is not None or self.std is not None:
            normalize(img_lq, self.mean, self.std, inplace=True)
            normalize(img_gt, self.mean, self.std, inplace=True)
            normalize(img_sample, self.mean, self.std, inplace=True)
        return {'hazy': img_lq, 'lq_path': lq_path, 'gt': img_gt, 'gt_path': gt_path, 'latent': latent_np[0], 'latent_path': np_path, 'sample': img_sample, 'sample_path': sample_path}

    def __len__(self):
        return len(self.gt_paths)
