# Copyright (c) OpenMMLab. All rights reserved.
import pickle
from copy import deepcopy
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
from mmengine import Config, MessageHub, print_log
from mmengine.model import is_model_wrapper
from mmengine.optim import OptimWrapper, OptimWrapperDict
from torch import Tensor

from mmagic.models.utils import get_module_device
from mmagic.registry import MODELS
from mmagic.structures import DataSample
from mmagic.utils import ForwardInputs, SampleList
from ...base_models import BaseGAN
from ...utils import set_requires_grad

ModelType = Union[Dict, nn.Module]
TrainInput = Union[dict, Tensor]


@MODELS.register_module()
class SinGAN(BaseGAN):
    """SinGAN.

    This model implement the single image generative adversarial model proposed
    in: Singan: Learning a Generative Model from a Single Natural Image,
    ICCV'19.

    Notes for training:

    - This model should be trained with our dataset ``SinGANDataset``.
    - In training, the ``total_iters`` arguments is related to the number of
      scales in the image pyramid and ``iters_per_scale`` in the ``train_cfg``.
      You should set it carefully in the training config file.

    Notes for model architectures:

    - The generator and discriminator need ``num_scales`` in initialization.
      However, this arguments is generated by ``create_real_pyramid`` function
      from the ``singan_dataset.py``. The last element in the returned list
      (``stop_scale``) is the value for ``num_scales``. Pay attention that this
      scale is counted from zero. Please see our tutorial for SinGAN to obtain
      more details or our standard config for reference.

    Args:
        generator (ModelType): The config or model of the generator.
        discriminator (Optional[ModelType]): The config or model of the
            discriminator. Defaults to None.
        data_preprocessor (Optional[Union[dict, Config]]): The pre-process
            config or :class:`~mmagic.models.DataPreprocessor`.
        generator_steps (int): The number of times the generator is completely
            updated before the discriminator is updated. Defaults to 1.
        discriminator_steps (int): The number of times the discriminator is
            completely updated before the generator is updated. Defaults to 1.
        num_scales (int): The number of scales/stages in generator/
            discriminator. Note that this number is counted from zero, which
            is the same as the original paper. Defaults to None.
        iters_per_scale (int): The training iteration for each resolution
            scale. Defaults to 2000.
        noise_weight_init (float): The initialize weight of fixed noise.
            Defaults to 0.1
        lr_scheduler_args (Optional[dict]): Arguments for learning schedulers.
            Note that in SinGAN, we use MultiStepLR, which is the same as the
            original paper. If not passed, no learning schedule will be used.
            Defaults to None.
        test_pkl_data (Optional[str]): The path of pickle file which contains
            fixed noise and noise weight. This is must for test. Defaults to
            None.
        ema_config (Optional[Dict]): The config for generator's exponential
            moving average setting. Defaults to None.
    """

    def __init__(self,
                 generator: ModelType,
                 discriminator: Optional[ModelType] = None,
                 data_preprocessor: Optional[Union[dict, Config]] = None,
                 generator_steps: int = 1,
                 discriminator_steps: int = 1,
                 num_scales: Optional[int] = None,
                 iters_per_scale: int = 2000,
                 noise_weight_init: int = 0.1,
                 lr_scheduler_args: Optional[dict] = None,
                 test_pkl_data: Optional[str] = None,
                 ema_confg: Optional[dict] = None):
        super().__init__(generator, discriminator, data_preprocessor,
                         generator_steps, discriminator_steps, None, ema_confg)
        self.iters_per_scale = iters_per_scale
        self.noise_weight_init = noise_weight_init
        if lr_scheduler_args:
            self.lr_scheduler_args = deepcopy(lr_scheduler_args)
        else:
            self.lr_scheduler_args = None

        # related to train
        self.num_scales = num_scales
        self.curr_stage = -1
        self.noise_weights = [1]
        self.fixed_noises = []
        self.reals = []

        # related to test
        self.loaded_test_pkl = False
        self.pkl_data = test_pkl_data

    def load_test_pkl(self):
        """Load pickle for test."""
        if self.pkl_data is not None:
            with open(self.pkl_data, 'rb') as f:
                data = pickle.load(f)
                self.fixed_noises = self._from_numpy(data['fixed_noises'])
                self.noise_weights = self._from_numpy(data['noise_weights'])
                self.curr_stage = data['curr_stage']
            print_log(f'Load pkl data from {self.pkl_data}', 'current')
            self.pkl_data = self.pkl_data
            self.loaded_test_pkl = True

    def _from_numpy(self,
                    data: Tuple[list,
                                np.ndarray]) -> Tuple[Tensor, List[Tensor]]:
        """Convert input numpy array or list of numpy array to Tensor or list
        of Tensor.

        Args:
            data (Tuple[list, np.ndarray]): Input data to convert.

        Returns:
            Tuple[Tensor, List[Tensor]]: Converted Tensor or list of tensor.
        """
        if isinstance(data, list):
            return [self._from_numpy(x) for x in data]

        if isinstance(data, np.ndarray):
            data = torch.from_numpy(data)
            device = get_module_device(self.generator)
            data = data.to(device)
            return data

        return data

    def get_module(self, model: nn.Module, module_name: str) -> nn.Module:
        """Get an inner module from model.

        Since we will wrapper DDP for some model, we have to judge whether the
        module can be indexed directly.

        Args:
            model (nn.Module): This model may wrapped with DDP or not.
            module_name (str): The name of specific module.

        Return:
            nn.Module: Returned sub module.
        """
        module = model.module if hasattr(model, 'module') else model
        return getattr(module, module_name)

    def construct_fixed_noises(self):
        """Construct the fixed noises list used in SinGAN."""
        for i, real in enumerate(self.reals):
            h, w = real.shape[-2:]
            if i == 0:
                noise = torch.randn(1, 1, h, w).to(real)
                self.fixed_noises.append(noise)
            else:
                noise = torch.zeros_like(real)
                self.fixed_noises.append(noise)

    def forward(self,
                inputs: ForwardInputs,
                data_samples: Optional[list] = None,
                mode=None) -> List[DataSample]:
        """Forward function for SinGAN. For SinGAN, `inputs` should be a dict
        contains 'num_batches', 'mode' and other input arguments for the
        generator.

        Args:
            inputs (dict): Dict containing the necessary information
                (e.g., noise, num_batches, mode) to generate image.
            data_samples (Optional[list]): Data samples collated by
                :attr:`data_preprocessor`. Defaults to None.
            mode (Optional[str]): `mode` is not used in
                :class:`BaseConditionalGAN`. Defaults to None.
        """

        # handle batch_inputs
        assert isinstance(inputs, dict), (
            'SinGAN only support dict type inputs in forward function.')
        gen_kwargs = deepcopy(inputs)
        num_batches = gen_kwargs.pop('num_batches', 1)
        assert num_batches == 1, (
            'SinGAN only support \'num_batches\' as 1, but receive '
            f'{num_batches}.')
        sample_model = self._get_valid_model(inputs)
        gen_kwargs.pop('sample_model', None)  # remove sample_model

        mode = gen_kwargs.pop('mode', mode)
        mode = 'rand' if mode is None else mode
        curr_scale = gen_kwargs.pop('curr_scale', self.curr_stage)

        self.fixed_noises = [
            x.to(self.data_preprocessor.device) for x in self.fixed_noises
        ]

        batch_sample_list = []
        if sample_model in ['ema', 'orig']:
            if sample_model == 'ema':
                generator = self.generator_ema
            else:
                generator = self.generator

            outputs = generator(
                None,
                fixed_noises=self.fixed_noises,
                noise_weights=self.noise_weights,
                rand_mode=mode,
                num_batches=1,
                curr_scale=curr_scale,
                **gen_kwargs)

            gen_sample = DataSample()
            # destruct
            if isinstance(outputs, dict):
                outputs['fake_img'] = self.data_preprocessor.destruct(
                    outputs['fake_img'], data_samples)
                outputs['prev_res_list'] = [
                    self.data_preprocessor.destruct(r, data_samples)
                    for r in outputs['prev_res_list']
                ]
                gen_sample.fake_img = self.data_preprocessor.destruct(
                    outputs['fake_img'], data_samples)
                # gen_sample.prev_res_list = self.data_preprocessor.destruct(
                #     outputs['fake_img'], data_samples)
            else:
                outputs = self.data_preprocessor.destruct(
                    outputs, data_samples)

            # save to data sample
            for idx in range(num_batches):
                gen_sample = DataSample()
                # save inputs to data sample
                if data_samples:
                    gen_sample.update(data_samples[idx])
                if isinstance(outputs, dict):
                    gen_sample.fake_img = outputs['fake_img'][idx]
                    gen_sample.prev_res_list = [
                        r[idx] for r in outputs['prev_res_list']
                    ]
                else:
                    gen_sample.fake_img = outputs[idx]

                gen_sample.sample_model = sample_model
                batch_sample_list.append(gen_sample)

        else:  # sample model is 'ema/orig'

            outputs_orig = self.generator(
                None,
                fixed_noises=self.fixed_noises,
                noise_weights=self.noise_weights,
                rand_mode=mode,
                num_batches=1,
                curr_scale=curr_scale,
                **gen_kwargs)
            outputs_ema = self.generator_ema(
                None,
                fixed_noises=self.fixed_noises,
                noise_weights=self.noise_weights,
                rand_mode=mode,
                num_batches=1,
                curr_scale=curr_scale,
                **gen_kwargs)

            # destruct
            if isinstance(outputs_orig, dict):
                outputs_orig['fake_img'] = self.data_preprocessor.destruct(
                    outputs_orig['fake_img'], data_samples)
                outputs_orig['prev_res_list'] = [
                    self.data_preprocessor.destruct(r, data_samples)
                    for r in outputs_orig['prev_res_list']
                ]
                outputs_ema['fake_img'] = self.data_preprocessor.destruct(
                    outputs_ema['fake_img'], data_samples)
                outputs_ema['prev_res_list'] = [
                    self.data_preprocessor.destruct(r, data_samples)
                    for r in outputs_ema['prev_res_list']
                ]
            else:
                outputs_orig = self.data_preprocessor.destruct(
                    outputs_orig, data_samples)
                outputs_ema = self.data_preprocessor.destruct(
                    outputs_ema, data_samples)

            # save to data sample
            for idx in range(num_batches):
                gen_sample = DataSample()
                gen_sample.ema = DataSample()
                gen_sample.orig = DataSample()
                # save inputs to data sample
                if data_samples:
                    gen_sample.update(data_samples[idx])
                if isinstance(outputs_orig, dict):
                    gen_sample.ema.fake_img = outputs_ema['fake_img'][idx]
                    gen_sample.ema.prev_res_list = [
                        r[idx] for r in outputs_ema['prev_res_list']
                    ]
                    gen_sample.orig.fake_img = outputs_orig['fake_img'][idx]
                    gen_sample.orig.prev_res_list = [
                        r[idx] for r in outputs_orig['prev_res_list']
                    ]
                else:
                    gen_sample.ema.fake_img = outputs_ema[idx]
                    gen_sample.orig.fake_img = outputs_orig[idx]
                gen_sample.sample_model = sample_model

                batch_sample_list.append(gen_sample)
        return batch_sample_list

    def gen_loss(self, disc_pred_fake: Tensor,
                 recon_imgs: Tensor) -> Tuple[Tensor, dict]:
        r"""Generator loss for SinGAN. SinGAN use WGAN's loss and MSE loss to
        train the generator.

        .. math:
            L_{D} = -\mathbb{E}_{z\sim{p_{z}}}D\left\(G\left\(z\right\)\right\)
                + L_{MSE}
            L_{MSE} = \text{mean} \Vert x - G(z) \Vert_2

        Args:
            disc_pred_fake (Tensor): Discriminator's prediction of the fake
                images.
            recon_imgs (Tensor): Reconstructive images.

        Returns:
            Tuple[Tensor, dict]: Loss value and a dict of log variables.
        """
        losses_dict = dict()
        losses_dict['loss_gen'] = -disc_pred_fake.mean()
        losses_dict['loss_mse'] = 10 * F.mse_loss(recon_imgs,
                                                  self.reals[self.curr_stage])
        loss, log_vars = self.parse_losses(losses_dict)
        return loss, log_vars

    def disc_loss(self, disc_pred_fake: Tensor, disc_pred_real: Tensor,
                  fake_data: Tensor, real_data: Tensor) -> Tuple[Tensor, dict]:
        r"""Get disc loss. SAGAN, SNGAN and Proj-GAN use hinge loss to train
        the generator.

        .. math:
            L_{D} = \mathbb{E}_{z\sim{p_{z}}}D\left\(G\left\(z\right\)\right\)
                - \mathbb{E}_{x\sim{p_{data}}}D\left\(x\right\) + L_{GP} \\
            L_{GP} = \lambda\mathbb{E}(\Vert\nabla_{\tilde{x}}D(\tilde{x})
                \Vert_2-1)^2 \\
            \tilde{x} = \epsilon x + (1-\epsilon)G(z)

        Args:
            disc_pred_fake (Tensor): Discriminator's prediction of the fake
                images.
            disc_pred_real (Tensor): Discriminator's prediction of the real
                images.
            fake_data (Tensor): Generated images, used to calculate gradient
                penalty.
            real_data (Tensor): Real images, used to calculate gradient
                penalty.

        Returns:
            Tuple[Tensor, dict]: Loss value and a dict of log variables.
        """

        losses_dict = dict()
        losses_dict['loss_disc_fake'] = disc_pred_fake.mean()
        losses_dict['loss_disc_real'] = -disc_pred_real.mean()

        # gradient penalty
        batch_size = real_data.size(0)
        alpha = torch.rand(batch_size, 1, 1, 1).to(real_data)

        # interpolate between real_data and fake_data
        interpolates = alpha * real_data + (1. - alpha) * fake_data
        interpolates = autograd.Variable(interpolates, requires_grad=True)

        disc_interpolates = self.discriminator(
            interpolates, curr_scale=self.curr_stage)
        gradients = autograd.grad(
            outputs=disc_interpolates,
            inputs=interpolates,
            grad_outputs=torch.ones_like(disc_interpolates),
            create_graph=True,
            retain_graph=True,
            only_inputs=True)[0]
        # norm_mode is 'pixel'
        gradients_penalty = ((gradients.norm(2, dim=1) - 1)**2).mean()
        losses_dict['loss_gp'] = 0.1 * gradients_penalty

        parsed_loss, log_vars = self.parse_losses(losses_dict)
        return parsed_loss, log_vars

    def train_generator(self, inputs: dict, data_samples: List[DataSample],
                        optimizer_wrapper: OptimWrapper) -> Dict[str, Tensor]:
        """Train generator.

        Args:
            inputs (dict): Inputs from dataloader.
            data_samples (List[DataSample]): Data samples from dataloader.
                Do not used in generator's training.
            optim_wrapper (OptimWrapper): OptimWrapper instance used to update
                model parameters.

        Returns:
            Dict[str, Tensor]: A ``dict`` of tensor for logging.
        """
        fake_imgs = self.generator(
            inputs['input_sample'],
            self.fixed_noises,
            self.noise_weights,
            rand_mode='rand',
            curr_scale=self.curr_stage)
        disc_pred_fake_g = self.discriminator(
            fake_imgs, curr_scale=self.curr_stage)

        recon_imgs = self.generator(
            inputs['input_sample'],
            self.fixed_noises,
            self.noise_weights,
            rand_mode='recon',
            curr_scale=self.curr_stage)

        parsed_loss, log_vars = self.gen_loss(disc_pred_fake_g, recon_imgs)
        optimizer_wrapper.update_params(parsed_loss)
        return log_vars

    def train_discriminator(self, inputs: dict, data_samples: List[DataSample],
                            optimizer_wrapper: OptimWrapper
                            ) -> Dict[str, Tensor]:
        """Train discriminator.

        Args:
            inputs (dict): Inputs from dataloader.
            data_samples (List[DataSample]): Data samples from dataloader.
            optim_wrapper (OptimWrapper): OptimWrapper instance used to update
                model parameters.
        Returns:
            Dict[str, Tensor]: A ``dict`` of tensor for logging.
        """
        input_sample = inputs['input_sample']
        fake_imgs = self.generator(
            input_sample,
            self.fixed_noises,
            self.noise_weights,
            rand_mode='rand',
            curr_scale=self.curr_stage)

        # disc pred for fake imgs and real_imgs
        real_imgs = self.reals[self.curr_stage]
        disc_pred_fake = self.discriminator(fake_imgs.detach(),
                                            self.curr_stage)
        disc_pred_real = self.discriminator(real_imgs, self.curr_stage)
        parsed_loss, log_vars = self.disc_loss(disc_pred_fake, disc_pred_real,
                                               fake_imgs, real_imgs)
        optimizer_wrapper.update_params(parsed_loss)
        return log_vars

    def train_gan(self, inputs_dict: dict, data_sample: List[DataSample],
                  optim_wrapper: OptimWrapperDict) -> Dict[str, torch.Tensor]:
        """Train GAN model. In the training of GAN models, generator and
        discriminator are updated alternatively. In MMagic's design,
        `self.train_step` is called with data input. Therefore we always update
        discriminator, whose updating is relay on real data, and then determine
        if the generator needs to be updated based on the current number of
        iterations. More details about whether to update generator can be found
        in :meth:`should_gen_update`.

        Args:
            data (dict): Data sampled from dataloader.
            data_sample (List[DataSample]): List of data sample contains GT
                and meta information.
            optim_wrapper (OptimWrapperDict): OptimWrapperDict instance
                contains OptimWrapper of generator and discriminator.

        Returns:
            Dict[str, torch.Tensor]: A ``dict`` of tensor for logging.
        """
        message_hub = MessageHub.get_current_instance()
        curr_iter = message_hub.get_info('iter')

        disc_optimizer_wrapper: OptimWrapper = optim_wrapper['discriminator']
        disc_accu_iters = disc_optimizer_wrapper._accumulative_counts

        with disc_optimizer_wrapper.optim_context(self.discriminator):
            log_vars = self.train_discriminator(inputs_dict, data_sample,
                                                disc_optimizer_wrapper)

        # add 1 to `curr_iter` because iter is updated in train loop.
        # Whether to update the generator. We update generator with
        # discriminator is fully updated for `self.n_discriminator_steps`
        # iterations. And one full updating for discriminator contains
        # `disc_accu_counts` times of grad accumulations.
        if (curr_iter + 1) % (self.discriminator_steps * disc_accu_iters) == 0:
            set_requires_grad(self.discriminator, False)
            gen_optimizer_wrapper = optim_wrapper['generator']
            gen_accu_iters = gen_optimizer_wrapper._accumulative_counts

            log_vars_gen_list = []
            # init optimizer wrapper status for generator manually
            gen_optimizer_wrapper.initialize_count_status(
                self.generator, 0, self.generator_steps * gen_accu_iters)
            for _ in range(self.generator_steps * gen_accu_iters):
                with gen_optimizer_wrapper.optim_context(self.generator):
                    log_vars_gen = self.train_generator(
                        inputs_dict, data_sample, gen_optimizer_wrapper)

                log_vars_gen_list.append(log_vars_gen)
            log_vars_gen = self.gather_log_vars(log_vars_gen_list)
            log_vars_gen.pop('loss', None)  # remove 'loss' from gen logs

            set_requires_grad(self.discriminator, True)

            # only do ema after generator update
            if self.with_ema_gen and (curr_iter + 1) >= (
                    self.ema_start * self.discriminator_steps *
                    disc_accu_iters):
                self.generator_ema.update_parameters(
                    self.generator.module
                    if is_model_wrapper(self.generator) else self.generator)
                # if not update buffer, copy buffer from orig model
                if not self.generator_ema.update_buffers:
                    self.generator_ema.sync_buffers(
                        self.generator.module if is_model_wrapper(
                            self.generator) else self.generator)
            elif self.with_ema_gen:
                # before ema, copy weights from orig
                self.generator_ema.sync_parameters(
                    self.generator.module
                    if is_model_wrapper(self.generator) else self.generator)

            log_vars.update(log_vars_gen)
        return log_vars

    def train_step(self, data: dict,
                   optim_wrapper: OptimWrapperDict) -> Dict[str, Tensor]:
        """Train step for SinGAN model. SinGAN is trained with multi-resolution
        images, and each resolution is trained for `:attr:self.iters_per_scale`
        times.

        We initialize the weight and learning rate scheduler of the
        corresponding module at the start of each resolution's training. At
        the end of each resolution's training, we update the weight of the
        noise of current resolution by mse loss between reconstruced image and
        real image.

        Args:
            data (dict): Data sampled from dataloader.
            optim_wrapper (OptimWrapperDict): OptimWrapperDict instance
                contains OptimWrapper of generator and discriminator.

        Returns:
            Dict[str, torch.Tensor]: A ``dict`` of tensor for logging.
        """

        message_hub = MessageHub.get_current_instance()
        curr_iter = message_hub.get_info('iter')

        if curr_iter % (self.iters_per_scale * self.discriminator_steps) == 0:
            self.curr_stage += 1
            # load weights from prev scale
            self.get_module(self.generator, 'check_and_load_prev_weight')(
                self.curr_stage)
            self.get_module(self.discriminator, 'check_and_load_prev_weight')(
                self.curr_stage)

            # assert grad_accumulation step is 1
            curr_gen_optim = optim_wrapper[f'generator_{self.curr_stage}']
            curr_disc_optim = optim_wrapper[f'discriminator_{self.curr_stage}']
            _warning_msg = ('SinGAN do set batch size as 1 during training '
                            'and do not support gradient accumulation.')
            assert curr_gen_optim._accumulative_counts == 1, _warning_msg
            assert curr_disc_optim._accumulative_counts == 1, _warning_msg

            # build parameters scheduler manually, because parameters_schedule
            # hook update all scheduler at the same
            if self.lr_scheduler_args:
                self.gen_scheduler = torch.optim.lr_scheduler.MultiStepLR(
                    curr_gen_optim.optimizer, **self.lr_scheduler_args)
                self.disc_scheduler = torch.optim.lr_scheduler.MultiStepLR(
                    curr_disc_optim.optimizer, **self.lr_scheduler_args)

        # get current optimizer_wrapper
        curr_optimizer_wrapper = OptimWrapperDict(
            generator=optim_wrapper[f'generator_{self.curr_stage}'],
            discriminator=optim_wrapper[f'discriminator_{self.curr_stage}'])

        # handle inputs
        data = self.data_preprocessor(data)
        inputs_dict, data_samples = data['inputs'], data['data_samples']
        # setup fixed noises and reals pyramid
        if curr_iter == 0 or len(self.reals) == 0:
            keys = [k for k in inputs_dict.keys() if 'real_scale' in k]
            scales = len(keys)
            self.reals = [inputs_dict[f'real_scale{s}'] for s in range(scales)]

            # here we do not padding fixed noises
            self.construct_fixed_noises()

        # standard train step
        log_vars = self.train_gan(inputs_dict, data_samples,
                                  curr_optimizer_wrapper)
        log_vars['curr_stage'] = self.curr_stage

        # update noise weight
        if ((curr_iter + 1) % (self.iters_per_scale * self.discriminator_steps)
                == 0) and (self.curr_stage < len(self.reals) - 1):
            with torch.no_grad():
                g_recon = self.generator(
                    inputs_dict['input_sample'],
                    self.fixed_noises,
                    self.noise_weights,
                    rand_mode='recon',
                    curr_scale=self.curr_stage)
                if isinstance(g_recon, dict):
                    g_recon = g_recon['fake_img']
                g_recon = F.interpolate(
                    g_recon, self.reals[self.curr_stage + 1].shape[-2:])

            mse = F.mse_loss(g_recon.detach(), self.reals[self.curr_stage + 1])
            rmse = torch.sqrt(mse)
            self.noise_weights.append(self.noise_weight_init * rmse.item())

        # call scheduler when all submodules are fully updated.
        if (curr_iter + 1) % self.discriminator_steps == 0:
            if self.lr_scheduler_args:
                self.disc_scheduler.step()
                self.gen_scheduler.step()

        return log_vars

    def test_step(self, data: dict) -> SampleList:
        """Gets the generated image of given data in test progress. Before
        generate images, we call `:meth:self.load_test_pkl` to load the fixed
        noise and current stage of the model from the pickle file.

        Args:
            data (dict): Data sampled from metric specific
                sampler. More detials in `Metrics` and `Evaluator`.

        Returns:
            SampleList: A list of ``DataSample`` contain generated results.
        """
        if not self.loaded_test_pkl:
            self.load_test_pkl()
        return super().test_step(data)
