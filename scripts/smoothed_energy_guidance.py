import logging
from os import environ
import math

import modules.scripts as scripts
import gradio as gr

from modules import script_callbacks
from modules.script_callbacks import CFGDenoiserParams, AfterCFGCallbackParams
from modules.processing import StableDiffusionProcessing
from modules import shared

from scripts.ui_wrapper import UIWrapper
from scripts.incant_utils import module_hooks

import torch
from torch.nn import functional as F

logger = logging.getLogger(__name__)
logger.setLevel(environ.get("SD_WEBUI_LOG_LEVEL", logging.INFO))

incantations_debug = environ.get("INCANTAIONS_DEBUG", False)

"""
An unofficial implementation of "Smoothed Energy Guidance for SDXL" for Automatic1111 WebUI.

@article{hong2024smoothed,
  title={Smoothed Energy Guidance: Guiding Diffusion Models with Reduced Energy Curvature of Attention},
  author={Hong, Susung},
  journal={arXiv preprint arXiv:2408.00760},
  year={2024}
}

Parts of the code are based off the author's official implementation at https://github.com/SusungHong/SEG-SDXL

Author: v0xie
GitHub URL: https://github.com/v0xie/sd-webui-incantations

"""


handles = []
global_scale = 1

class SEGStateParams:
        def __init__(self):
                self.seg_active: bool = False      # SEG guidance scale
                self.seg_scale: int = -1      # SEG guidance scale
                self.seg_blur_sigma: float = 1.0
                self.seg_blur_threshold: float = 15.0 # 2^13 ~= 8192
                self.seg_start_step: int = 0
                self.seg_end_step: int = 150 
                self.crossattn_modules = [] # callable lambda


class SEGExtensionScript(UIWrapper):
        def __init__(self):
                self.cached_c = [None, None]
                self.paste_field_names = []
                self.infotext_fields = []
                self.handles = []

        # Extension title in menu UI
        def title(self) -> str:
                return "Smoothed Energy Guidance"

        # Decide to show menu in txt2img or img2img
        def show(self, is_img2img):
                return scripts.AlwaysVisible

        # Setup menu ui detail
        def setup_ui(self, is_img2img) -> list:
                with gr.Accordion('Smoothed Energy Guidance', open=True):
                        active = gr.Checkbox(value=False, default=False, label="Active", elem_id='seg_active', info="Recommended to keep CFG Scale fixed at 3.0, use Sigma to adjust.")
                        with gr.Row():
                                seg_blur_sigma = gr.Slider(value = 11.0, minimum = 0.0, maximum = 11.0, step = 0.5, label="SEG Blur Sigma", elem_id = 'seg_blur_sigma', info="Exponential (2^n). Values >= 11 are infinite blur")
                        with gr.Row():
                                start_step = gr.Slider(value = 0, minimum = 0, maximum = 150, step = 1, label="Start Step", elem_id = 'seg_start_step', info="")
                                end_step = gr.Slider(value = 150, minimum = 0, maximum = 150, step = 1, label="End Step", elem_id = 'seg_end_step', info="")

                params = [active, seg_blur_sigma, start_step, end_step]
                                
                self.infotext_fields = [
                        (active, lambda d: gr.Checkbox.update(value='SEG Active' in d)),
                        (seg_blur_sigma, 'SEG Blur Sigma'),
                        (start_step, 'SEG Start Step'),
                        (end_step, 'SEG End Step'),
                ]
                for p in params:
                        p.do_not_save_to_config = True
                        self.paste_field_names.append(p.elem_id)

                return params

        def process_batch(self, p: StableDiffusionProcessing, *args, **kwargs):
               self.seg_process_batch(p, *args, **kwargs)

        def seg_process_batch(self, p: StableDiffusionProcessing, active, seg_blur_sigma, start_step, end_step, *args, **kwargs):
                # cleanup previous hooks always
                script_callbacks.remove_current_script_callbacks()
                self.remove_all_hooks()

                active = getattr(p, "seg_active", active)
                if active is False:
                        return
                seg_blur_sigma = getattr(p, "seg_blur_sigma", seg_blur_sigma)
                if seg_blur_sigma == 0.0:
                        logger.info("SEG Blur Sigma is 0, skipping SEG")
                        return
                start_step = getattr(p, "seg_start_step", start_step)
                end_step = getattr(p, "seg_end_step", end_step)

                if active:
                        p.extra_generation_params.update({
                                "SEG Active": active,
                                "SEG Blur Sigma": seg_blur_sigma,
                                "SEG Start Step": start_step,
                                "SEG End Step": end_step,
                        })
                self.create_hook(p, active, seg_blur_sigma, start_step, end_step)

        def create_hook(self, p: StableDiffusionProcessing, active, seg_blur_sigma, start_step, end_step, *args, **kwargs):
                # Create a list of parameters for each concept
                seg_params = SEGStateParams()

                # Add to p's incant_cfg_params
                if not hasattr(p, 'incant_cfg_params'):
                        logger.error("No incant_cfg_params found in p")
                p.incant_cfg_params['seg_params'] = seg_params
                
                seg_params.seg_active = active 
                seg_params.seg_blur_sigma = seg_blur_sigma
                seg_params.seg_blur_threshold = 10.5
                seg_params.seg_start_step = start_step
                seg_params.seg_end_step = end_step

                # Get all the qv modules
                self_attn_modules = self.get_cross_attn_modules()
                if len(self_attn_modules) == 0:
                        logger.error("No self attention modules found, cannot proceed with SEG")
                        return
                seg_params.crossattn_modules = self_attn_modules

                cfg_denoise_lambda = lambda callback_params: self.on_cfg_denoiser_callback(callback_params, seg_params)
                unhook_lambda = lambda _: self.unhook_callbacks(seg_params)

                if seg_params.seg_active:
                        self.ready_hijack_forward(seg_params.crossattn_modules, seg_blur_sigma, seg_params.seg_blur_threshold, p.height, p.width)

                logger.debug('Hooked callbacks')
                script_callbacks.on_cfg_denoiser(cfg_denoise_lambda)
                script_callbacks.on_script_unloaded(unhook_lambda)

        def postprocess_batch(self, p, *args, **kwargs):
                self.seg_postprocess_batch(p, *args, **kwargs)

        def seg_postprocess_batch(self, p, active, seg_blur_sigma, start_step, end_step, *args, **kwargs):
                script_callbacks.remove_current_script_callbacks()

                logger.debug('Removed script callbacks')
                active = getattr(p, "seg_active", active)
                if active is False:
                        return

        def remove_all_hooks(self):
                self_attn_modules = self.get_cross_attn_modules()
                for module in self_attn_modules:
                        module_hooks.modules_remove_field(module.to_q, 'seg_enable')
                        module_hooks.modules_remove_field(module.to_q, 'seg_parent_module')
                        module_hooks.remove_module_forward_hook(module.to_q, 'seg_to_q_hook')

        def unhook_callbacks(self, seg_params: SEGStateParams):
                global handles
                return

        def ready_hijack_forward(self, selfattn_modules, seg_blur_sigma, seg_blur_threshold, height, width):
                for module in selfattn_modules:
                        module_hooks.modules_add_field(module.to_q, 'seg_enable', False)
                        module_hooks.modules_add_field(module.to_q, 'seg_parent_module', [module])

                def seg_to_q_hook(module, input, kwargs, output):
                        if not hasattr(module, 'seg_enable'):
                                return
                        if not module.seg_enable:
                                return
                        batch_size, seq_len, inner_dim = input[0].shape
                        h = module.seg_parent_module[0].heads
                        head_dim = inner_dim // h

                        module_attn_size = seq_len
                        downscale_h = int((module_attn_size * (height / width)) ** 0.5)
                        downscale_w = module_attn_size // downscale_h

                        # actual sigma value is calculated as 2 ^ sigma
                        is_inf_blur = seg_blur_sigma > seg_blur_threshold
                        blur_sigma_exp = 2 ** seg_blur_sigma
                        kernel_size = math.ceil(6 * blur_sigma_exp) + 1 - math.ceil(6 * blur_sigma_exp) % 2

                        q_uncond, q= output.chunk(2, dim=0) 
                        q = q.view(batch_size//2, -1, h, head_dim).transpose(1, 2) # (batch, num_heads, seq_len, head_dim)
                        q = q.permute(0, 1, 3, 2).reshape(batch_size//2 * h, head_dim, downscale_h, downscale_w) # (batch * num_heads, head_dim, height, width)

                        if is_inf_blur:
                                q = gaussian_blur_inf(q, 1.0, blur_sigma_exp)
                        else:
                                q = gaussian_blur_2d(q, kernel_size, blur_sigma_exp)

                        q = q.reshape(batch_size // 2, h, head_dim, downscale_h * downscale_w) # (batch, num_heads, head_dim, seq_len)
                        q = q.view(batch_size // 2, h * head_dim, seq_len).transpose(1, 2) # (batch, inner_dim, seq_len)
                        q = torch.cat((q_uncond, q), dim=0)

                        return q

                # Create hooks 
                for module in selfattn_modules:
                        module_hooks.module_add_forward_hook(module.to_q, seg_to_q_hook, hook_type="forward", with_kwargs=True)

        def get_middle_block_modules(self):
                """ Get all attention modules from the middle block 
                Refere to page 22 of the SEG paper, Appendix A.2
                
                """
                middle_block_modules = module_hooks.get_modules(
                        network_layer_name_filter = 'middle_block_',
                        module_name_filter = 'CrossAttention'
                )
                middle_block_modules = [m for m in middle_block_modules if 'attn1' in m.network_layer_name]
                return middle_block_modules

        def get_cross_attn_modules(self):
                """ Get all cross attention modules """
                return self.get_middle_block_modules()

        def on_cfg_denoiser_callback(self, params: CFGDenoiserParams, seg_params: SEGStateParams):
                # always unhook
                self.unhook_callbacks(seg_params)
                if not seg_params.seg_active:
                        return

                in_interval = seg_params.seg_start_step <= params.sampling_step <= seg_params.seg_end_step
                for module in seg_params.crossattn_modules:
                        if hasattr(module.to_q, 'seg_enable'):
                                module.to_q.seg_enable = in_interval

        def cfg_after_cfg_callback(self, params: AfterCFGCallbackParams, seg_params: SEGStateParams):
                pass

        def get_xyz_axis_options(self) -> dict:
                xyz_grid = [x for x in scripts.scripts_data if x.script_class.__module__ in ("xyz_grid.py", "scripts.xyz_grid")][0].module
                extra_axis_options = {
                        xyz_grid.AxisOption("[SEG] Active", str, seg_apply_override('seg_active', boolean=True), choices=xyz_grid.boolean_choice(reverse=True)),
                        xyz_grid.AxisOption("[SEG] SEG Blur Sigma", float, seg_apply_field("seg_blur_sigma")),
                        xyz_grid.AxisOption("[SEG] SEG Start Step", int, seg_apply_field("seg_start_step")),
                        xyz_grid.AxisOption("[SEG] SEG End Step", int, seg_apply_field("seg_end_step")),
                }
                return extra_axis_options


# from modules/sd_samplers_cfg_denoiser.py:187-195
def get_make_condition_dict_fn(text_uncond):
        if shared.sd_model.model.conditioning_key == "crossattn-adm":
                make_condition_dict = lambda c_crossattn, c_adm: {"c_crossattn": [c_crossattn], "c_adm": c_adm}
        else:
                if isinstance(text_uncond, dict):
                        make_condition_dict = lambda c_crossattn, c_concat: {**c_crossattn, "c_concat": [c_concat]}
                else:
                        make_condition_dict = lambda c_crossattn, c_concat: {"c_crossattn": [c_crossattn], "c_concat": [c_concat]}
        return make_condition_dict


# XYZ Plot
# Based on @mcmonkey4eva's XYZ Plot implementation here: https://github.com/mcmonkeyprojects/sd-dynamic-thresholding/blob/master/scripts/dynamic_thresholding.py
def seg_apply_override(field, boolean: bool = False):
    def fun(p, x, xs):
        if boolean:
            x = True if x.lower() == "true" else False
        setattr(p, field, x)
        if not hasattr(p, "seg_active"):
                setattr(p, "seg_active", True)
        if 'cfg_interval_' in field and not hasattr(p, "cfg_interval_enable"):
            setattr(p, "cfg_interval_enable", True)
    return fun


def seg_apply_field(field):
    def fun(p, x, xs):
        if not hasattr(p, "seg_active"):
                setattr(p, "seg_active", True)
        setattr(p, field, x)
    return fun


# Gaussian blur
# taken from https://github.com/SusungHong/SEG-SDXL/blob/master/pipeline_seg.py
def gaussian_blur_2d(img, kernel_size, sigma):
        height = img.shape[-1]
        kernel_size = min(kernel_size, height - (height % 2 - 1))
        ksize_half = (kernel_size - 1) * 0.5

        x = torch.linspace(-ksize_half, ksize_half, steps=kernel_size)

        pdf = torch.exp(-0.5 * (x / sigma).pow(2))

        x_kernel = pdf / pdf.sum()
        x_kernel = x_kernel.to(device=img.device, dtype=img.dtype)

        kernel2d = torch.mm(x_kernel[:, None], x_kernel[None, :])
        kernel2d = kernel2d.expand(img.shape[-3], 1, kernel2d.shape[0], kernel2d.shape[1])

        padding = [kernel_size // 2, kernel_size // 2, kernel_size // 2, kernel_size // 2]

        img = F.pad(img, padding, mode="reflect")
        img = F.conv2d(img, kernel2d, groups=img.shape[-3])

        return img


def gaussian_blur_inf(img, kernel_size, sigma):
        img[:] = img.mean(dim=(-2, -1), keepdim=True)

        return img