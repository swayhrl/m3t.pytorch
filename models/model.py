# coding=utf-8
# Copyright 2020 Yuan-Hang Zhang and Rulin Huang.
#
from .dataset import AffWild2SequenceDataset
from .backbone import *
from .rnn import GRU, AttEncDec
from .att_fusion import AttFusion

from .utils import concordance_cc2, mse
from .lr_finder import BatchExponentialLR, plot_lr

from argparse import ArgumentParser

import torch
from torch.nn import functional as F
from torch.utils.data import DataLoader

import pytorch_lightning as pl

import sys


LR_TEST_MAX_LR = 0.01
LR_TEST_STEPS = 1096 * 3


class AffWild2VA(pl.LightningModule):
    
    def __init__(self, hparams):
        super(AffWild2VA, self).__init__()
        self.hparams = hparams

        use_mtl = 'mtl' in self.hparams.loss
        # Expr(, AU), VA
        fc_outputs = 7 + 2 if use_mtl else 2 # 7 + 8 + 2 if use_mtl else 2
        if self.hparams.modality == 'audiovisual':
            rnn_fc_classes = -1
        else:
            rnn_fc_classes = fc_outputs

        if 'visual' in self.hparams.modality:
            if self.hparams.backbone == 'resnet':
                self.visual = VA_3DResNet(
                    hiddenDim=self.hparams.num_hidden,
                    frameLen=self.hparams.window,
                    backend=self.hparams.backend,
                    resnet_ver='v1',
                    nClasses=rnn_fc_classes,
                    nFCs=self.hparams.num_fc_layers
                )
            elif self.hparams.backbone == 'v2p':
                self.visual = VA_3DVGGM(
                    hiddenDim=self.hparams.num_hidden,
                    frameLen=self.hparams.window,
                    backend=self.hparams.backend,
                    nClasses=rnn_fc_classes,
                    nFCs=self.hparams.num_fc_layers
                )
            elif self.hparams.backbone == 'v2p_split':
                self.visual = VA_3DVGGM_Split(
                    hiddenDim=self.hparams.num_hidden,
                    frameLen=self.hparams.window,
                    backend=self.hparams.backend,
                    split_layer=self.hparams.split_layer,
                    nClasses=rnn_fc_classes,
                    nFCs=self.hparams.num_fc_layers,
                    use_mtl=use_mtl
                )
            elif self.hparams.backbone == 'densenet':
                self.visual = VA_3DDenseNet(
                    hiddenDim=self.hparams.num_hidden,
                    frameLen=self.hparams.window,
                    backend=self.hparams.backend,
                    nClasses=rnn_fc_classes,
                    nFCs=self.hparams.num_fc_layers
                )
            elif self.hparams.backbone == 'vggface':
                self.visual = VA_VGGFace(
                    hiddenDim=self.hparams.num_hidden,
                    frameLen=self.hparams.window,
                    backend=self.hparams.backend,
                    nClasses=rnn_fc_classes,
                    nFCs=self.hparams.num_fc_layers
                )
        if 'audio' in self.hparams.modality:
            self.audio = GRU(200, 256, 2, rnn_fc_classes, self.hparams.num_fc_layers)# , dropout=True)
        if self.hparams.modality == 'audiovisual':
            self.proj_v = nn.Linear(self.hparams.num_hidden * (2 if self.hparams.split_layer == 5 else 4), 512)
            if self.hparams.fusion_type == 'attention':
                self.att_fuse = AttFusion([512, 512], 128)
                self.fusion = GRU(512,
                self.hparams.num_hidden, 2, fc_outputs, self.hparams.num_fc_layers)
            elif self.hparams.fusion_type == 'concat':
                self.fusion = GRU(512 * 2,
                self.hparams.num_hidden, 2, fc_outputs, self.hparams.num_fc_layers)
            elif self.hparams.fusion_type == 'att_dec':
                self.fusion = AttEncDec()

        self.history = {'lr': [], 'loss': []}

    def forward(self, batch):
        if self.hparams.modality == 'audio':
            return self.audio(batch['audio'])
        else:
            # normalize video to [-1, 1]
            x = (batch['video'] - 127.5) / 127.5
            # audiovisual
            if 'audio' in self.hparams.modality:
                audio_feats = self.audio(batch['audio'])
                # video_feats = self.visual(x, batch['se_features'], batch['au_features'])
                video_feats = self.visual(x, batch['se_features'], batch['se_features'])
                video_feats = self.proj_v(video_feats)
                if self.hparams.fusion_type == 'concat':
                    features = torch.cat((audio_feats, video_feats), dim=-1)
                    return self.fusion(features)
                elif self.hparams.fusion_type == 'attention':
                    features = self.att_fuse(audio_feats, video_feats)
                    return self.fusion(features)
                elif self.hparams.fusion_type == 'att_dec':
                    features = torch.cat((audio_feats, video_feats), dim=-1)
                    if 'arousal' in batch.keys():
                        trg = torch.stack((batch['valence'], batch['arousal']), dim=-1)
                        predictions = self.fusion(features, trg)
                    else:
                        predictions = self.fusion(features)
                    return predictions
            # visual
            else:
                # return self.visual(x, batch['se_features'], batch['au_features'])
                return self.visual(x, batch['se_features'], batch['se_features'])
    
    def ccc_loss(self, y_hat, y):
        return 1 - concordance_cc2(y_hat.view(-1), y.view(-1), 'none').squeeze()
    
    def bce_loss(self, y_hat, y, mask):
        loss = F.binary_cross_entropy_with_logits(y_hat.view(-1), y.view(-1), reduction='none')
        return (loss * mask.view(-1).float()).mean()
    
    def ce_loss(self, y_hat, y, mask):
        loss = F.cross_entropy(y_hat.view(-1, y_hat.size(-1)), y.view(-1), reduction='none')
        return (loss * mask.view(-1).float()).mean()
    
    def mse_loss(self, y_hat, y):
        return F.mse_loss(y_hat, y)
    
    def training_step(self, batch, batch_idx):
        arousal = batch['label_arousal']
        valence = batch['label_valence']
        
        y_hat = self.forward(batch)
        if 'mtl' in self.hparams.loss:
            # y = (x_v: Expr(6), V; x_a: [AU(8),] A)
            valence_hat, arousal_hat = y_hat[..., 7], y_hat[..., -1]
        else:
            valence_hat, arousal_hat = y_hat[..., -2], y_hat[..., -1]

        if 'mse' in self.hparams.loss:
            loss_v = self.mse_loss(valence_hat, valence)
            loss_a = self.mse_loss(arousal_hat, arousal)
        else:
            assert 'ccc' in self.hparams.loss, 'invalid loss specification'
            loss_v = self.ccc_loss(valence_hat, valence)
            loss_a = self.ccc_loss(arousal_hat, arousal)
        loss = self.hparams.loss_lambda * loss_v + (1 - self.hparams.loss_lambda) * loss_a

        progress_dict = {'loss_v': loss_v, 'loss_a': loss_a, 'loss': loss}
        log_dict = {'loss_v': loss_v, 'loss_a': loss_a, 'loss': loss}

        if 'mtl' in self.hparams.loss:
            # expression branch
            mask_expr = batch['expr_valid']
            mask_expr_tile = mask_expr.view(-1)
            valid_expr = torch.sum(mask_expr_tile.long()).item()
            if valid_expr > 0:
                expr_hat, expr = y_hat[..., :7], batch['class_expr']
                loss_expr = self.ce_loss(expr_hat, expr, mask_expr)
                loss += 0.8 * loss_expr # balancing factor
                log_dict['loss_expr'] = loss_expr
                progress_dict['loss_expr'] = loss_expr
                max_expr_class = torch.argmax(expr_hat, dim=-1).view(-1)
                acc_expr = torch.sum(max_expr_class[mask_expr_tile] == expr.view(-1)[mask_expr_tile]).item() / valid_expr
                progress_dict['acc_expr'] = acc_expr
            # AU detection branch
            '''
            mask_au = batch['au_valid']
            mask_au_tile = mask_au.view(-1)
            valid_au = torch.sum(mask_au_tile.long()).item()
            if valid_au > 0:
                au_hat, au = y_hat[..., 8: 16], batch['class_au']
                loss_au = 0
                for i in range(8):
                    loss_au += self.bce_loss(au_hat[..., i], au[..., i].float(), mask_au)
                    max_class = torch.sigmoid(au_hat[..., i]).view(-1) > 0.5
                    acc = torch.sum(max_class[mask_au_tile].long() == au[..., i].view(-1)[mask_au_tile]).item() / valid_au
                    progress_dict['acc_au{}'.format(i+1)] = acc
                loss += loss_au
                log_dict['loss_au'] = loss_au
                progress_dict['loss_au'] = loss_au
            '''
        
        if self.hparams.test_lr:
            if len(self.history['lr']) == LR_TEST_STEPS:
                plot_lr(self.history)
                print ('Saved LR-loss plot.')
                sys.exit(0)
            else:
                lr = self.lr_test.get_lr()[0]
                self.history['lr'].append(lr)
                if batch_idx != 0: # smoothing
                    self.history['loss'].append(0.05 * loss.item() + 0.95 * self.history['loss'][-1])
                else:
                    self.history['loss'].append(loss.item())

        return {
            'loss': loss,
            'progress_bar': progress_dict,
            'log': log_dict
        }
    
    def on_batch_end(self):
        if self.hparams.test_lr:
            self.lr_test.step()
        if self.hparams.scheduler == 'cyclic':
            self.cyclic_scheduler.step()

    def validation_step(self, batch, batch_idx):
        v, a, v_hat, a_hat = [], [], [], []
        
        y_hat = self.forward(batch).cpu()
        valence_hat, arousal_hat = y_hat[..., -2], y_hat[..., -1]
        lens = batch['length']

        bs = lens.size(0)
        v_hat.extend([valence_hat[i][: lens[i]] for i in range(bs)])
        a_hat.extend([arousal_hat[i][: lens[i]] for i in range(bs)])
        
        valence, arousal = batch['label_valence'].cpu(), batch['label_arousal'].cpu()
        v.extend([valence[i][: lens[i]] for i in range(bs)])
        a.extend([arousal[i][: lens[i]] for i in range(bs)])

        return {
            'v_gt': v, 'a_gt': a,
            'v_pred': v_hat, 'a_pred': a_hat,
            'vid_names': batch['vid_name'],
            'start_frames': batch['start'].cpu()
        }

    def validation_end(self, outputs):
        all_v_gt = torch.cat([torch.cat(x['v_gt']) for x in outputs])
        all_a_gt = torch.cat([torch.cat(x['a_gt']) for x in outputs])
        all_v_pred = torch.cat([torch.cat(x['v_pred']) for x in outputs])
        all_a_pred = torch.cat([torch.cat(x['a_pred']) for x in outputs])

        is_valid = (torch.abs(all_v_gt) <= 1) & (torch.abs(all_a_gt) <= 1)
        all_ccc_v = concordance_cc2(all_v_gt[is_valid], all_v_pred[is_valid])
        all_ccc_a = concordance_cc2(all_a_gt[is_valid], all_a_pred[is_valid])
        all_mse_v = mse(all_v_pred[is_valid], all_v_gt[is_valid])
        all_mse_a = mse(all_a_pred[is_valid], all_a_gt[is_valid])

        val_loss = 1 - 0.5 * (all_ccc_v + all_ccc_a)

        # save outputs for visualisation
        predictions = {}
        for x in outputs:
            # gather batch elements by file name
            for vid_name, st_frame, v_gt, a_gt, v_pred, a_pred in zip(x['vid_names'], x['start_frames'], x['v_gt'], x['a_gt'], x['v_pred'], x['a_pred']):
                if vid_name in predictions.keys():
                    predictions[vid_name].append((st_frame, v_gt, a_gt, v_pred, a_pred))
                else:
                    predictions[vid_name] = [(st_frame, v_gt, a_gt, v_pred, a_pred)]
        pred_v, pred_a, gt_v, gt_a = {}, {}, {}, {}
        for k, w in predictions.items():
            # sort segment predictions by start frame index
            sorted_preds = sorted(w)
            if not self.hparams.test_on_val:
                gt_v[k] = torch.cat([x[1] for x in sorted_preds])
                gt_a[k] = torch.cat([x[2] for x in sorted_preds])
                pred_v[k] = torch.cat([x[3] for x in sorted_preds])
                pred_a[k] = torch.cat([x[4] for x in sorted_preds])
            else:
                nframes = sorted_preds[-1][0] + len(sorted_preds[-1][1])
                pred_v[k] = torch.zeros(nframes)
                pred_a[k] = torch.zeros(nframes)
                # TODO(yuanhang): fetch gt directly from dataset?
                gt_v[k] = torch.zeros(nframes)
                gt_a[k] = torch.zeros(nframes)
                for x in sorted_preds:
                    seg_nframes = len(x[1])
                    gt_v[k][x[0]: x[0] + seg_nframes] += x[1]
                    gt_a[k][x[0]: x[0] + seg_nframes] += x[2]
                    pred_v[k][x[0]: x[0] + seg_nframes] += x[3]
                    pred_a[k][x[0]: x[0] + seg_nframes] += x[4]
                # accounting for the overlaps
                gt_v[k][self.hparams.window // 2: ] /= 2.
                gt_a[k][self.hparams.window // 2: ] /= 2.
                pred_v[k][self.hparams.window // 2: ] /= 2.
                pred_a[k][self.hparams.window // 2: ] /= 2.
        torch.save({
            'valence_gt': gt_v,
            'arousal_gt': gt_a,
            'valence_pred': pred_v,
            'arousal_pred': pred_a
        }, 'predictions_val.pt')

        return {
            'val_loss': val_loss,
            'progress_bar': {
                'val_ccc_v': all_ccc_v,
                'val_ccc_a': all_ccc_a
            },
            'log': {
                'val_ccc_v': all_ccc_v,
                'val_ccc_a': all_ccc_a,
                'val_mse_v': all_mse_v,
                'val_mse_a': all_mse_a,
                'val_loss': val_loss
            }
        }
    
    def test_step(self, batch, batch_idx):
        if self.hparams.test_on_val:
            return self.validation_step(batch, batch_idx)
        v_hat, a_hat = [], []
        
        y_hat = self.forward(batch).cpu()
        valence_hat, arousal_hat = y_hat[..., -2], y_hat[..., -1]
        lens = batch['length']

        bs = lens.size(0)
        v_hat.extend([valence_hat[i][: lens[i]] for i in range(bs)])
        a_hat.extend([arousal_hat[i][: lens[i]] for i in range(bs)])
        
        return {
            'v_pred': v_hat, 'a_pred': a_hat,
            'vid_names': batch['vid_name'],
            'start_frames': batch['start'].cpu()
        }

    def test_end(self, outputs):
        if self.hparams.test_on_val:
            return self.validation_end(outputs)
        predictions = {}
        for x in outputs:
            # gather batch elements by file name
            for vid_name, st_frame, v, a in zip(x['vid_names'], x['start_frames'], x['v_pred'], x['a_pred']):
                if vid_name in predictions.keys():
                    predictions[vid_name].append((st_frame, v, a))
                else:
                    predictions[vid_name] = [(st_frame, v, a)]
        pred_v, pred_a = {}, {}
        for k, w in predictions.items():
            # sort segment predictions by start frame index
            sorted_preds = sorted(w)
            # DEPRECATED: stride = wlen
            # pred_v[k] = torch.cat([x[1] for x in sorted_preds])
            # pred_a[k] = torch.cat([x[2] for x in sorted_preds])
            # stride = wlen / 2
            nframes = sorted_preds[-1][0] + len(sorted_preds[-1][1])
            pred_v[k] = torch.zeros(nframes)
            pred_a[k] = torch.zeros(nframes)
            for x in sorted_preds:
                seg_nframes = len(x[1])
                pred_v[k][x[0]: x[0] + seg_nframes] += x[1]
                pred_a[k][x[0]: x[0] + seg_nframes] += x[2]
            pred_v[k][self.hparams.window // 2: ] /= 2.
            pred_a[k][self.hparams.window // 2: ] /= 2.
        # save predictions for further ensembling
        torch.save({
            'valence_pred': pred_v,
            'arousal_pred': pred_a
        }, 'predictions_test.pt')
        
        return {}

    def configure_optimizers(self):
        if self.hparams.freeze_enc:
            for param in self.parameters():
                param.requires_grad = False
            for param in self.fusion.parameters():
                param.requires_grad = True
            for param in self.proj_v.parameters():
                param.requires_grad = True
            if self.hparams.fusion_type == 'attention':
                for param in self.att_fuse.parameters():
                    param.requires_grad = True

        if self.hparams.optimizer == 'adam':
            optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad, self.parameters()),
                                         lr=self.hparams.learning_rate,
                                         weight_decay=1e-4)
        elif self.hparams.optimizer == 'sgd':
            optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, self.parameters()),
                                        lr=self.hparams.learning_rate,
                                        momentum=0.9, weight_decay=5e-4)
        if self.hparams.test_lr:
            self.lr_test = BatchExponentialLR(optimizer, LR_TEST_MAX_LR, LR_TEST_STEPS)
            return optimizer
        else:
            if self.hparams.scheduler == 'cyclic':
                self.cyclic_scheduler = torch.optim.lr_scheduler.CyclicLR(optimizer, self.hparams.min_lr, self.hparams.learning_rate, step_size_up=5000, cycle_momentum=self.hparams.optimizer == 'sgd')
                return optimizer
            elif self.hparams.scheduler == 'exp':
                scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, self.hparams.decay_factor)
                return [optimizer], [scheduler]
            elif self.hparams.scheduler == 'plateau':
                scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=self.hparams.decay_factor, patience=3, verbose=True, min_lr=1e-6)
                return [optimizer], [scheduler]
    
    @pl.data_loader
    def train_dataloader(self):
        if self.hparams.mode == 'video':
            dataset = AffWild2SequenceDataset('train', self.hparams.dataset_path, self.hparams.window, self.hparams.windows_per_epoch, self.hparams.cutout, self.hparams.release, self.hparams.input_size, self.hparams.modality, self.hparams.resample)
        else:
            # TODO: implement framewise
            raise NotImplementedError
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=True, num_workers=self.hparams.workers, pin_memory=True)

    @pl.data_loader
    def val_dataloader(self):
        if self.hparams.mode == 'video':
            dataset = AffWild2SequenceDataset('val', self.hparams.dataset_path, self.hparams.window, self.hparams.windows_per_epoch, self.hparams.cutout, self.hparams.release, self.hparams.input_size, self.hparams.modality, self.hparams.resample, 2 if self.hparams.test_on_val else 1)
        else:
            raise NotImplementedError
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.workers, pin_memory=True)

    @pl.data_loader
    def test_dataloader(self):
        if self.hparams.test_on_val:
            return self.val_dataloader()
        if self.hparams.mode == 'video':
            dataset = AffWild2SequenceDataset('test', self.hparams.dataset_path, self.hparams.window, self.hparams.windows_per_epoch, self.hparams.cutout, self.hparams.release, self.hparams.input_size, self.hparams.modality, self.hparams.resample, 2) # stride = 1/2 window_len
        else:
            raise NotImplementedError
        if self.hparams.distributed:
            dist_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
            return DataLoader(dataset, batch_size=self.hparams.batch_size, num_workers=self.hparams.workers, pin_memory=True, sampler=dist_sampler)
        else:
            return DataLoader(dataset, batch_size=self.hparams.batch_size, shuffle=False, num_workers=self.hparams.workers, pin_memory=True)

    @staticmethod
    def add_model_specific_args(parent_parser):
        """
        Specify the hyperparams for this LightningModule
        """
        # MODEL specific
        parser = ArgumentParser(parents=[parent_parser])
        parser.add_argument('--backbone', default='v2p_split', type=str)
        parser.add_argument('--backend', default='gru', type=str)

        parser.add_argument('--modality', default='visual', type=str)
        parser.add_argument('--fusion_type', default='concat', type=str)
        parser.add_argument('--freeze_enc', action='store_true', default=False)

        parser.add_argument('--resample', action='store_true', default=False)
        parser.add_argument('--mode', default='video', type=str)
        parser.add_argument('--window', default=32, type=int)
        parser.add_argument('--windows_per_epoch', default=200, type=int)

        parser.add_argument('--learning_rate', default=5e-5, type=float)
        parser.add_argument('--min_lr', default=1e-8, type=float)
        parser.add_argument('--decay_factor', default=0.5, type=float)
        parser.add_argument('--batch_size', default=96, type=int)
        parser.add_argument('--optimizer', default='adam', type=str)
        parser.add_argument('--scheduler', default='plateau', type=str)
        parser.add_argument('--test_lr', action='store_true', default=False)

        parser.add_argument('--test_on_val', action='store_true', default=False)

        parser.add_argument('--loss', default='ccc_mtl', type=str)
        parser.add_argument('--loss_lambda', default=0.5, type=float)
        parser.add_argument('--num_hidden', default=512, type=int)
        parser.add_argument('--split_layer', default=3, type=int)
        parser.add_argument('--num_fc_layers', default=2, type=int)
        parser.add_argument('--cutout', action='store_true', default=False)

        # training specific (for this model)
        parser.add_argument('--distributed', action='store_true', default=False)
        parser.add_argument('--dataset_path', default='/.data/zhangyuanhang/Aff-Wild2', type=str)
        parser.add_argument('--release', default='vipl', type=str)
        parser.add_argument('--input_size', default=256, type=int)
        parser.add_argument('--checkpoint_path', default='.', type=str)
        parser.add_argument('--workers', default=8, type=int)
        parser.add_argument('--max_nb_epochs', default=30, type=int)

        return parser
