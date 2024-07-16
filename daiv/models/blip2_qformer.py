import logging

import torch
import torch.nn as nn
import torch.nn.functional as F

from daiv.common.registry import registry
from daiv.models.base_model import all_gather_with_grad, concat_all_gather
from daiv.models.blip2 import Blip2Base, compute_sim_matrix, disabled_train
from daiv.models.blip_outputs import BlipOutput, BlipOutputFeatures

from daiv.models.dmformer.mcan.net import Net  # Importing the Net class from net.py
from daiv.models.dmformer.mcan.net_utils import LayerNorm  # Importing LayerNorm

@registry.register_model("blip2")
@registry.register_model("blip2_feature_extractor")

class Blip2Qformer(Blip2Base):
    """
    BLIP2 first-stage model with MCAN and ViT.
    """

    PRETRAINED_MODEL_CONFIG_DICT = {
        "pretrain": "configs/models/blip2_pretrain.yaml",
        "pretrain_vitL": "configs/models/blip2/blip2_pretrain_vitL.yaml",
        "coco": "configs/models/blip2/blip2_coco.yaml",
    }

    class Config:
        HIDDEN_SIZE = 512
        DROPOUT_R = 0.1
        MULTI_HEAD = 8
        HIDDEN_SIZE_HEAD = HIDDEN_SIZE // MULTI_HEAD
        FF_SIZE = 2048
        LAYER = 6
        FLAT_MLP_SIZE = 512
        FLAT_GLIMPSES = 1
        FLAT_OUT_SIZE = 512
        WORD_EMBED_SIZE = 300
        USE_GLOVE = False
        IMG_FEAT_SIZE = 1408  # This should match the output feature size of the visual encoder

    def __init__(
        self,
        vit_model="eva_clip_g",
        img_size=224,
        drop_path_rate=0,
        use_grad_checkpoint=False,
        vit_precision="fp16",
        freeze_vit=True,
        num_query_token=32,
        cross_attention_freq=2,
        embed_dim=512,
        max_txt_len=32,
    ):
        super().__init__()

        self.tokenizer = self.init_tokenizer()

        # Initialize the vision encoder
        self.visual_encoder, self.ln_vision = self.init_vision_encoder(
            vit_model, img_size, drop_path_rate, use_grad_checkpoint, vit_precision
        )
        if freeze_vit:
            for name, param in self.visual_encoder.named_parameters():
                param.requires_grad = False
            self.visual_encoder = self.visual_encoder.eval()
            self.visual_encoder.train = disabled_train
            logging.info("freeze vision encoder")

        # Initialize MCAN instead of Q-former
        self.MCAN = Net(self.Config, pretrained_emb=None, token_size=10000, answer_size=embed_dim)  # Adjust arguments as necessary

        self.vision_proj = nn.Linear(self.Config.IMG_FEAT_SIZE, embed_dim)
        self.text_proj = nn.Linear(self.Config.WORD_EMBED_SIZE, embed_dim)  # Adjusted to match the embedding size

        self.itm_head = nn.Linear(embed_dim, 2)

        self.temp = nn.Parameter(0.07 * torch.ones([]))

        self.max_txt_len = max_txt_len

    def forward(self, samples):
        print('MCAN training....')
        image = samples["image"]
        text = samples["text_input"]

        image_embeds = self.ln_vision(self.visual_encoder(image))
        print(f'image_embeds size after vision encoder: {image_embeds.size()}')
        image_embeds = self.vision_proj(image_embeds)  # Project image features to the correct size
        print(f'image_embeds size after vision projection: {image_embeds.size()}')

        image_atts = torch.ones(image_embeds.size()[:-1], dtype=torch.long).to(image.device)

        # Tokenize text and pass through embedding layer
        text_tokens = self.tokenizer(text, return_tensors="pt", padding=True, truncation=True, max_length=self.max_txt_len).input_ids.to(image.device)
        text_embeds = self.MCAN.embedding(text_tokens)
        print(f'text_embeds size after tokenization and embedding: {text_embeds.size()}')

        # Project text features to the correct size
        text_embeds = self.text_proj(text_embeds.view(-1, text_embeds.size(-1))).view(text_embeds.size(0), text_embeds.size(1), -1)
        print(f'text_embeds size after projection: {text_embeds.size()}')

        lang_feat_mask = self.MCAN.make_mask(text_tokens)
        img_feat_mask = self.MCAN.make_mask(image_embeds)
        print(f'lang_feat_mask size: {lang_feat_mask.size()}')
        print(f'img_feat_mask size: {img_feat_mask.size()}')

        # Using MCAN
        lang_feat, img_feat = self.MCAN.backbone(text_embeds, image_embeds, lang_feat_mask, img_feat_mask)
        img_feat = self.MCAN.attflat_img(img_feat, img_feat_mask)
        lang_feat = self.MCAN.attflat_lang(lang_feat, lang_feat_mask)

        image_feats = F.normalize(img_feat, dim=-1)
        text_feat = F.normalize(lang_feat, dim=-1)

        ###============== Image-text Contrastive ===================###
        image_feats_all = concat_all_gather(image_feats)
        text_feat_all = concat_all_gather(text_feat)

        sim_q2t = torch.matmul(image_feats.unsqueeze(1), text_feat_all.unsqueeze(-1)).squeeze()
        sim_i2t, _ = sim_q2t.max(-1)
        sim_i2t = sim_i2t / self.temp

        sim_t2q = torch.matmul(text_feat.unsqueeze(1).unsqueeze(1), image_feats_all.permute(0, 2, 1)).squeeze()
        sim_t2i, _ = sim_t2q.max(-1)
        sim_t2i = sim_t2i / self.temp

        rank = 0  # dist.get_rank()

        bs = image.size(0)
        targets = torch.linspace(rank * bs, rank * bs + bs - 1, bs, dtype=int).to(image.device)

        if "image_id" in samples.keys():
            image_ids = samples["image_id"].view(-1, 1)
            image_ids_all = concat_all_gather(image_ids)
            pos_idx = torch.eq(image_ids, image_ids_all.t()).float()
            sim_targets = pos_idx / pos_idx.sum(1, keepdim=True)
            sim_targets = 0.9 * sim_targets + 0.1 * torch.ones_like(sim_targets) / sim_targets.size(1)

            loss_t2i = -torch.sum(F.log_softmax(sim_t2i, dim=1) * sim_targets, dim=1).mean()
            loss_i2t = -torch.sum(F.log_softmax(sim_i2t, dim=1) * sim_targets, dim=1).mean()
            loss_itc = (loss_t2i + loss_i2t) / 2
        else:
            loss_itc = (
                F.cross_entropy(sim_i2t, targets, label_smoothing=0.1)
                + F.cross_entropy(sim_t2i, targets, label_smoothing=0.1)
            ) / 2

        ###============== Image-text Matching ===================###
        text_input_ids_world = concat_all_gather(text_embeds)
        text_attention_mask_world = concat_all_gather(lang_feat_mask)
        image_embeds_world = all_gather_with_grad(image_embeds)
        with torch.no_grad():
            if "image_id" in samples.keys():
                mask = torch.eq(image_ids, image_ids_all.t())
                sim_t2i.masked_fill_(mask, -10000)
                sim_i2t.masked_fill_(mask, -10000)
            else:
                sim_t2i[:, rank * bs: rank * bs + bs].fill_diagonal_(-10000)
                sim_i2t[:, rank * bs: rank * bs + bs].fill_diagonal_(-10000)

            weights_t2i = F.softmax(sim_t2i, dim=1)
            weights_i2t = F.softmax(sim_i2t, dim=1)

        # Select a negative image for each text
        image_embeds_neg = []
        for b in range(bs):
            neg_idx = torch.multinomial(weights_t2i[b], 1).item()
            image_embeds_neg.append(image_embeds_world[neg_idx])
        image_embeds_neg = torch.stack(image_embeds_neg, dim=0)
        print(f'image_embeds_neg size: {image_embeds_neg.size()}')

        # Select a negative text for each image
        text_ids_neg = []
        text_atts_neg = []
        for b in range(bs):
            neg_idx = torch.multinomial(weights_i2t[b], 1).item()
            text_ids_neg.append(text_input_ids_world[neg_idx])
            text_atts_neg.append(text_attention_mask_world[neg_idx])

        text_ids_neg = torch.stack(text_ids_neg, dim=0)
        text_atts_neg = torch.stack(text_atts_neg, dim=0)
        print(f'text_ids_neg size: {text_ids_neg.size()}')
        print(f'text_atts_neg size: {text_atts_neg.size()}')

        text_ids_all = torch.cat([text_embeds, text_embeds, text_ids_neg], dim=0)
        text_atts_all = torch.cat([lang_feat_mask, lang_feat_mask, text_atts_neg], dim=0)
        print(f'text_ids_all size: {text_ids_all.size()}')
        print(f'text_atts_all size: {text_atts_all.size()}')

        query_tokens_itm = torch.zeros((text_ids_all.shape[0], self.max_txt_len, image_embeds.shape[-1])).to(image.device)
        query_atts_itm = torch.ones(query_tokens_itm.size()[:-1], dtype=torch.long).to(image.device)
        attention_mask_all = torch.cat([query_atts_itm, text_atts_all], dim=1)
        print(f'query_tokens_itm size: {query_tokens_itm.size()}')
        print(f'query_atts_itm size: {query_atts_itm.size()}')
        print(f'attention_mask_all size: {attention_mask_all.size()}')

        image_embeds_all = torch.cat([image_embeds, image_embeds_neg, image_embeds], dim=0)
        image_atts_all = torch.ones(image_embeds_all.size()[:-1], dtype=torch.long).to(image.device)
        print(f'image_embeds_all size: {image_embeds_all.size()}')
        print(f'image_atts_all size: {image_atts_all.size()}')

        output_itm = self.MCAN.backbone(
            text_ids_all,
            query_tokens_itm,
            text_atts_all,
            image_embeds_all,
            attention_mask_all,
            return_dict=True,
        )

        vl_embeddings = output_itm.last_hidden_state[:, :query_tokens_itm.size(1), :]
        vl_output = self.itm_head(vl_embeddings)
        logits = vl_output.mean(dim=1)

        itm_labels = torch.cat(
            [torch.ones(bs, dtype=torch.long), torch.zeros(2 * bs, dtype=torch.long)],
            dim=0,
        ).to(image.device)
        loss_itm = F.cross_entropy(logits, itm_labels)

        ##================= Image Captioning ========================##
        decoder_input_ids = text_embeds.clone()
        decoder_input_ids[:, 0] = self.tokenizer.bos_token_id
        labels = decoder_input_ids.masked_fill(decoder_input_ids == self.tokenizer.pad_token_id, -100)

        query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(image.device)
        attention_mask = torch.cat([query_atts, lang_feat_mask], dim=1)
        lm_output = self.MCAN.backbone(
            decoder_input_ids,
            query_tokens,
            attention_mask,
            past_key_values=query_output.past_key_values,
            return_dict=True,
            labels=labels,
        )

        loss_lm = lm_output.loss

        return BlipOutput(
            loss=loss_itc + loss_itm + loss_lm,
            loss_itc=loss_itc,
            loss_itm=loss_itm,
            loss_lm=loss_lm,
        )

    @torch.no_grad()
    def generate(
        self,
        samples,
        use_nucleus_sampling=False,
        num_beams=3,
        max_length=30,
        min_length=10,
        top_p=0.9,
        repetition_penalty=1.0,
    ):
        image = samples["image"]
        image_embeds = self.ln_vision(self.visual_encoder(image))
        image_embeds = self.vision_proj(image_embeds)  # Project image features to the correct size

        if not use_nucleus_sampling:
            image_embeds = image_embeds.repeat_interleave(num_beams, dim=0)
        else:
            num_beams = 1
        image_atts = torch.ones(image_embeds.size()[:-1], dtype=torch.long).to(image.device)

        model_kwargs = {
            "encoder_hidden_states": image_embeds,
            "encoder_attention_mask": image_atts,
        }

        input_ids = (
            torch.LongTensor(image.size(0), 1)
            .fill_(self.tokenizer.bos_token_id)
            .to(image.device)
        )
        query_tokens = torch.zeros((image_embeds.shape[0], self.max_txt_len, image_embeds.shape[-1])).to(image.device)

        outputs = self.MCAN.generate(
            input_ids=input_ids,
            query_embeds=query_tokens,
            max_length=max_length,
            min_length=min_length,
            num_beams=num_beams,
            do_sample=use_nucleus_sampling,
            top_p=top_p,
            eos_token_id=self.tokenizer.sep_token_id,
            pad_token_id=self.tokenizer.pad_token_id,
            **model_kwargs
        )
        captions = self.tokenizer.batch_decode(outputs, skip_special_tokens=True)
        return captions

    def forward_image(self, image):
        image_embeds = self.ln_vision(self.visual_encoder(image))
        image_embeds = self.vision_proj(image_embeds)  # Project image features to the correct size
        image_atts = torch.ones(image_embeds.size()[:-1], dtype=torch.long).to(image.device)

        query_tokens = torch.zeros((image_embeds.shape[0], self.max_txt_len, image_embeds.shape[-1])).to(image.device)

        query_output = self.MCAN.backbone(
            query_tokens,
            image_embeds,
            image_atts,
            return_dict=True,
        )
        return query_output.last_hidden_state, image_embeds

    def forward_text(self, text_tokens):
        text_embeds = self.MCAN.embedding(text_tokens.input_ids.to(self.device))
        text_output = self.MCAN.backbone(
            text_embeds,
            text_tokens.attention_mask,
            return_dict=True,
        )
        return text_output.last_hidden_state[:, 0, :]

    def compute_itm(self, image_inputs, text_ids, text_atts):
        image_inputs = self.vision_proj(image_inputs)  # Project image features to the correct size
        image_atts = torch.ones(image_inputs.size()[:-1], dtype=torch.long).to(image_inputs.device)
        query_tokens = torch.zeros((image_inputs.shape[0], self.max_txt_len, image_inputs.shape[-1])).to(image_inputs.device)
        query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(image_inputs.device)
        attention_mask = torch.cat([query_atts, text_atts], dim=1)
        output_itm = self.MCAN.backbone(
            text_ids,
            query_tokens,
            attention_mask,
            encoder_hidden_states=image_inputs,
            encoder_attention_mask=image_atts,
            return_dict=True,
        )
        vl_embeddings = output_itm.last_hidden_state[:, :query_tokens.size(1), :]
        itm_logit = self.itm_head(vl_embeddings)
        itm_logit = itm_logit[:, :, 1].mean(dim=1)
        return itm_logit

    @torch.no_grad()
    def extract_features(self, samples, mode="multimodal"):
        image = samples.get("image")
        caption = samples.get("text_input")

        assert mode in ["image", "text", "multimodal"], "mode must be one of 'image', 'text', 'multimodal'"

        image_embeds, text_embeds, multimodal_embeds = None, None, None
        image_features, text_features = None, None

        if mode == "image":
            assert image is not None, "Image is not provided for mode 'image' or 'multimodal'"
            with self.maybe_autocast():
                image_embeds_frozen = self.ln_vision(self.visual_encoder(image))
            image_embeds_frozen = image_embeds_frozen.float()
            image_embeds_frozen = self.vision_proj(image_embeds_frozen)  # Project image features to the correct size
            image_atts = torch.ones(image_embeds_frozen.size()[:-1], dtype=torch.long).to(self.device)
            query_tokens = torch.zeros((image_embeds_frozen.shape[0], self.max_txt_len, image_embeds_frozen.shape[-1])).to(self.device)

            query_output = self.MCAN.backbone(
                query_tokens,
                image_embeds_frozen,
                image_atts,
                return_dict=True,
            )
            image_embeds = query_output.last_hidden_state
            image_features = F.normalize(self.vision_proj(image_embeds), dim=-1)

        elif mode == "text":
            assert caption is not None, "text input is None for mode 'text' or 'multimodal'"

            text = self.tokenizer(caption, return_tensors="pt", padding=True).to(self.device)
            text_embeds = self.MCAN.embedding(text.input_ids)

            text_output = self.MCAN.backbone(
                text_embeds,
                text.attention_mask,
                return_dict=True,
            )
            text_embeds = text_output.last_hidden_state
            text_features = self.text_proj(text_embeds)
            text_features = F.normalize(text_features, dim=-1)

        elif mode == "multimodal":
            with self.maybe_autocast():
                image_embeds_frozen = self.ln_vision(self.visual_encoder(image))
            image_embeds_frozen = image_embeds_frozen.float()
            image_embeds_frozen = self.vision_proj(image_embeds_frozen)  # Project image features to the correct size
            image_atts = torch.ones(image_embeds_frozen.size()[:-1], dtype=torch.long).to(self.device)
            query_tokens = torch.zeros((image_embeds_frozen.shape[0], self.max_txt_len, image_embeds_frozen.shape[-1])).to(self.device)
            query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(self.device)

            text = self.tokenizer(caption, return_tensors="pt", padding=True).to(self.device)
            text_embeds = self.MCAN.embedding(text.input_ids)
            attention_mask = torch.cat([query_atts, text.attention_mask], dim=1)

            output = self.MCAN.backbone(
                text_embeds,
                query_tokens,
                attention_mask,
                encoder_hidden_states=image_embeds_frozen,
                encoder_attention_mask=image_atts,
                return_dict=True,
            )

            multimodal_embeds = output.last_hidden_state[:, :query_tokens.size(1), :]

        return BlipOutputFeatures(
            image_embeds=image_embeds,
            image_embeds_proj=image_features,
            text_embeds=text_embeds,
            text_embeds_proj=text_features,
            multimodal_embeds=multimodal_embeds,
        )

    @classmethod
    def from_config(cls, cfg):
        vit_model = cfg.get("vit_model", "eva_clip_g")
        img_size = cfg.get("image_size")
        num_query_token = cfg.get("num_query_token")
        cross_attention_freq = cfg.get("cross_attention_freq", 2)

        drop_path_rate = cfg.get("drop_path_rate", 0)
        use_grad_checkpoint = cfg.get("use_grad_checkpoint", False)
        vit_precision = cfg.get("vit_precision", "fp16")
        freeze_vit = cfg.get("freeze_vit", True)

        max_txt_len = cfg.get("max_txt_len", 32)

        model = cls(
            vit_model=vit_model,
            img_size=img_size,
            drop_path_rate=drop_path_rate,
            use_grad_checkpoint=use_grad_checkpoint,
            vit_precision=vit_precision,
            freeze_vit=freeze_vit,
            num_query_token=num_query_token,
            cross_attention_freq=cross_attention_freq,
            max_txt_len=max_txt_len,
        )
        model.load_checkpoint_from_config(cfg)

        return model

    def compute_sim_matrix(self, data_loader, task_cfg):
        k_test = task_cfg.k_test

        return compute_sim_matrix(model=self, data_loader=data_loader, k_test=k_test)
