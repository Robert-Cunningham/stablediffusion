import collections
import os.path
import sys
import gc
from collections import namedtuple
import torch
import re
import safetensors.torch
from omegaconf import OmegaConf
from os import mkdir
from urllib import request
import ldm.modules.midas as midas

from ldm.util import instantiate_from_config

from modules import shared, modelloader, devices, script_callbacks, sd_vae
from modules.paths import models_path
from modules.sd_hijack_inpainting import do_inpainting_hijack, should_hijack_inpainting

model_dir = "Stable-diffusion"
model_path = os.path.abspath(os.path.join(models_path, model_dir))

CheckpointInfo = namedtuple("CheckpointInfo", ['filename', 'title', 'hash', 'model_name'])
checkpoints_list = {}
checkpoints_loaded = collections.OrderedDict()

try:
    # this silences the annoying "Some weights of the model checkpoint were not used when initializing..." message at start.

    from transformers import logging, CLIPModel

    logging.set_verbosity_error()
except Exception:
    pass


def setup_model():
    if not os.path.exists(model_path):
        os.makedirs(model_path)

    list_models()
    enable_midas_autodownload()


def checkpoint_tiles(): 
    convert = lambda name: int(name) if name.isdigit() else name.lower() 
    alphanumeric_key = lambda key: [convert(c) for c in re.split('([0-9]+)', key)] 
    return sorted([x.title for x in checkpoints_list.values()], key = alphanumeric_key)


def find_checkpoint_config(info):
    config = os.path.splitext(info.filename)[0] + ".yaml"
    if os.path.exists(config):
        return config

    return shared.cmd_opts.config


def list_models():
    checkpoints_list.clear()
    model_list = modelloader.load_models(model_path=model_path, command_path=shared.cmd_opts.ckpt_dir, ext_filter=[".ckpt", ".safetensors"])

    def modeltitle(path, shorthash):
        abspath = os.path.abspath(path)

        if shared.cmd_opts.ckpt_dir is not None and abspath.startswith(shared.cmd_opts.ckpt_dir):
            name = abspath.replace(shared.cmd_opts.ckpt_dir, '')
        elif abspath.startswith(model_path):
            name = abspath.replace(model_path, '')
        else:
            name = os.path.basename(path)

        if name.startswith("\\") or name.startswith("/"):
            name = name[1:]

        shortname = os.path.splitext(name.replace("/", "_").replace("\\", "_"))[0]

        return f'{name} [{shorthash}]', shortname

    cmd_ckpt = shared.cmd_opts.ckpt
    if os.path.exists(cmd_ckpt):
        h = model_hash(cmd_ckpt)
        title, short_model_name = modeltitle(cmd_ckpt, h)
        checkpoints_list[title] = CheckpointInfo(cmd_ckpt, title, h, short_model_name)
        shared.opts.data['sd_model_checkpoint'] = title
    elif cmd_ckpt is not None and cmd_ckpt != shared.default_sd_model_file:
        print(f"Checkpoint in --ckpt argument not found (Possible it was moved to {model_path}: {cmd_ckpt}", file=sys.stderr)
    for filename in model_list:
        h = model_hash(filename)
        title, short_model_name = modeltitle(filename, h)

        checkpoints_list[title] = CheckpointInfo(filename, title, h, short_model_name)


def get_closet_checkpoint_match(searchString):
    applicable = sorted([info for info in checkpoints_list.values() if searchString in info.title], key = lambda x:len(x.title))
    if len(applicable) > 0:
        return applicable[0]
    return None


def model_hash(filename):
    try:
        with open(filename, "rb") as file:
            import hashlib
            m = hashlib.sha256()

            file.seek(0x100000)
            m.update(file.read(0x10000))
            return m.hexdigest()[0:8]
    except FileNotFoundError:
        return 'NOFILE'


def select_checkpoint():
    model_checkpoint = shared.opts.sd_model_checkpoint
        
    checkpoint_info = checkpoints_list.get(model_checkpoint, None)
    if checkpoint_info is not None:
        return checkpoint_info

    if len(checkpoints_list) == 0:
        print("No checkpoints found. When searching for checkpoints, looked at:", file=sys.stderr)
        if shared.cmd_opts.ckpt is not None:
            print(f" - file {os.path.abspath(shared.cmd_opts.ckpt)}", file=sys.stderr)
        print(f" - directory {model_path}", file=sys.stderr)
        if shared.cmd_opts.ckpt_dir is not None:
            print(f" - directory {os.path.abspath(shared.cmd_opts.ckpt_dir)}", file=sys.stderr)
        print("Can't run without a checkpoint. Find and place a .ckpt file into any of those locations. The program will exit.", file=sys.stderr)
        exit(1)

    checkpoint_info = next(iter(checkpoints_list.values()))
    if model_checkpoint is not None:
        print(f"Checkpoint {model_checkpoint} not found; loading fallback {checkpoint_info.title}", file=sys.stderr)

    return checkpoint_info


chckpoint_dict_replacements = {
    'cond_stage_model.transformer.embeddings.': 'cond_stage_model.transformer.text_model.embeddings.',
    'cond_stage_model.transformer.encoder.': 'cond_stage_model.transformer.text_model.encoder.',
    'cond_stage_model.transformer.final_layer_norm.': 'cond_stage_model.transformer.text_model.final_layer_norm.',
}


def transform_checkpoint_dict_key(k):
    for text, replacement in chckpoint_dict_replacements.items():
        if k.startswith(text):
            k = replacement + k[len(text):]

    return k


def get_state_dict_from_checkpoint(pl_sd):
    pl_sd = pl_sd.pop("state_dict", pl_sd)
    pl_sd.pop("state_dict", None)

    sd = {}
    for k, v in pl_sd.items():
        new_key = transform_checkpoint_dict_key(k)

        if new_key is not None:
            sd[new_key] = v

    pl_sd.clear()
    pl_sd.update(sd)

    return pl_sd


def read_state_dict(checkpoint_file, print_global_state=False, map_location=None):
    _, extension = os.path.splitext(checkpoint_file)
    if extension.lower() == ".safetensors":
        device = map_location or shared.weight_load_location
        if device is None:
            device = devices.get_cuda_device_string() if torch.cuda.is_available() else "cpu"
        pl_sd = safetensors.torch.load_file(checkpoint_file, device=device)
    else:
        pl_sd = torch.load(checkpoint_file, map_location=map_location or shared.weight_load_location)

    if print_global_state and "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")

    sd = get_state_dict_from_checkpoint(pl_sd)
    return sd


def load_model_weights(model, checkpoint_info, vae_file="auto"):
    checkpoint_file = checkpoint_info.filename
    sd_model_hash = checkpoint_info.hash

    cache_enabled = shared.opts.sd_checkpoint_cache > 0

    if cache_enabled and checkpoint_info in checkpoints_loaded:
        # use checkpoint cache
        print(f"Loading weights [{sd_model_hash}] from cache")
        model.load_state_dict(checkpoints_loaded[checkpoint_info])
    else:
        # load from file
        print(f"Loading weights [{sd_model_hash}] from {checkpoint_file}")

        sd = read_state_dict(checkpoint_file)
        model.load_state_dict(sd, strict=False)
        del sd
        
        if cache_enabled:
            # cache newly loaded model
            checkpoints_loaded[checkpoint_info] = model.state_dict().copy()

        if shared.cmd_opts.opt_channelslast:
            model.to(memory_format=torch.channels_last)

        if not shared.cmd_opts.no_half:
            vae = model.first_stage_model

            # with --no-half-vae, remove VAE from model when doing half() to prevent its weights from being converted to float16
            if shared.cmd_opts.no_half_vae:
                model.first_stage_model = None

            model.half()
            model.first_stage_model = vae

        devices.dtype = torch.float32 if shared.cmd_opts.no_half else torch.float16
        devices.dtype_vae = torch.float32 if shared.cmd_opts.no_half or shared.cmd_opts.no_half_vae else torch.float16

        model.first_stage_model.to(devices.dtype_vae)

    # clean up cache if limit is reached
    if cache_enabled:
        while len(checkpoints_loaded) > shared.opts.sd_checkpoint_cache + 1: # we need to count the current model
            checkpoints_loaded.popitem(last=False)  # LRU

    model.sd_model_hash = sd_model_hash
    model.sd_model_checkpoint = checkpoint_file
    model.sd_checkpoint_info = checkpoint_info

    model.logvar = model.logvar.to(devices.device)  # fix for training

    sd_vae.delete_base_vae()
    sd_vae.clear_loaded_vae()
    vae_file = sd_vae.resolve_vae(checkpoint_file, vae_file=vae_file)
    sd_vae.load_vae(model, vae_file)


def enable_midas_autodownload():
    """
    Gives the ldm.modules.midas.api.load_model function automatic downloading.

    When the 512-depth-ema model, and other future models like it, is loaded,
    it calls midas.api.load_model to load the associated midas depth model.
    This function applies a wrapper to download the model to the correct
    location automatically.
    """

    midas_path = os.path.join(models_path, 'midas')

    # stable-diffusion-stability-ai hard-codes the midas model path to
    # a location that differs from where other scripts using this model look.
    # HACK: Overriding the path here.
    for k, v in midas.api.ISL_PATHS.items():
        file_name = os.path.basename(v)
        midas.api.ISL_PATHS[k] = os.path.join(midas_path, file_name)

    midas_urls = {
        "dpt_large": "https://github.com/intel-isl/DPT/releases/download/1_0/dpt_large-midas-2f21e586.pt",
        "dpt_hybrid": "https://github.com/intel-isl/DPT/releases/download/1_0/dpt_hybrid-midas-501f0c75.pt",
        "midas_v21": "https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21-f6b98070.pt",
        "midas_v21_small": "https://github.com/AlexeyAB/MiDaS/releases/download/midas_dpt/midas_v21_small-70d6b9c8.pt",
    }

    midas.api.load_model_inner = midas.api.load_model

    def load_model_wrapper(model_type):
        path = midas.api.ISL_PATHS[model_type]
        if not os.path.exists(path):
            if not os.path.exists(midas_path):
                mkdir(midas_path)
    
            print(f"Downloading midas model weights for {model_type} to {path}")
            request.urlretrieve(midas_urls[model_type], path)
            print(f"{model_type} downloaded")

        return midas.api.load_model_inner(model_type)

    midas.api.load_model = load_model_wrapper


def load_model(checkpoint_info=None):
    from modules import lowvram, sd_hijack
    checkpoint_info = checkpoint_info or select_checkpoint()
    checkpoint_config = find_checkpoint_config(checkpoint_info)

    if checkpoint_config != shared.cmd_opts.config:
        print(f"Loading config from: {checkpoint_config}")

    if shared.sd_model:
        sd_hijack.model_hijack.undo_hijack(shared.sd_model)
        shared.sd_model = None
        gc.collect()
        devices.torch_gc()

    sd_config = OmegaConf.load(checkpoint_config)
    
    if should_hijack_inpainting(checkpoint_info):
        # Hardcoded config for now...
        sd_config.model.target = "ldm.models.diffusion.ddpm.LatentInpaintDiffusion"
        sd_config.model.params.conditioning_key = "hybrid"
        sd_config.model.params.unet_config.params.in_channels = 9
        sd_config.model.params.finetune_keys = None

    if not hasattr(sd_config.model.params, "use_ema"):
        sd_config.model.params.use_ema = False

    do_inpainting_hijack()

    if shared.cmd_opts.no_half:
        sd_config.model.params.unet_config.params.use_fp16 = False

    sd_model = instantiate_from_config(sd_config.model)

    load_model_weights(sd_model, checkpoint_info)

    if shared.cmd_opts.lowvram or shared.cmd_opts.medvram:
        lowvram.setup_for_low_vram(sd_model, shared.cmd_opts.medvram)
    else:
        sd_model.to(shared.device)

    sd_hijack.model_hijack.hijack(sd_model)

    sd_model.eval()
    shared.sd_model = sd_model

    sd_hijack.model_hijack.embedding_db.load_textual_inversion_embeddings(force_reload=True)  # Reload embeddings after model load as they may or may not fit the model

    script_callbacks.model_loaded_callback(sd_model)

    print("Model loaded.")

    return sd_model


def reload_model_weights(sd_model=None, info=None):
    from modules import lowvram, devices, sd_hijack
    checkpoint_info = info or select_checkpoint()

    if not sd_model:
        sd_model = shared.sd_model

    current_checkpoint_info = sd_model.sd_checkpoint_info
    checkpoint_config = find_checkpoint_config(current_checkpoint_info)

    if sd_model.sd_model_checkpoint == checkpoint_info.filename:
        return

    if checkpoint_config != find_checkpoint_config(checkpoint_info) or should_hijack_inpainting(checkpoint_info) != should_hijack_inpainting(sd_model.sd_checkpoint_info):
        del sd_model
        checkpoints_loaded.clear()
        load_model(checkpoint_info)
        return shared.sd_model

    if shared.cmd_opts.lowvram or shared.cmd_opts.medvram:
        lowvram.send_everything_to_cpu()
    else:
        sd_model.to(devices.cpu)

    sd_hijack.model_hijack.undo_hijack(sd_model)

    try:
        load_model_weights(sd_model, checkpoint_info)
    except Exception as e:
        print("Failed to load checkpoint, restoring previous")
        load_model_weights(sd_model, current_checkpoint_info)
        raise
    finally:
        sd_hijack.model_hijack.hijack(sd_model)
        script_callbacks.model_loaded_callback(sd_model)

        if not shared.cmd_opts.lowvram and not shared.cmd_opts.medvram:
            sd_model.to(devices.device)

    print("Weights loaded.")

    return sd_model
