import torch.nn as nn
from transformers import AutoModelForCausalLM, PreTrainedModel, PretrainedConfig
from transformers.modeling_outputs import CausalLMOutput
import torch.nn.functional as F
from transformers import AutoTokenizer
import copy
import warnings
from dataclasses import dataclass
from typing import Optional, Union, List, Tuple, Callable

import numpy as np
import torch
from torch import nn
import torch.distributed as dist
import torch.nn.functional as F
from transformers import (
    LogitsProcessorList, 
    StoppingCriteriaList,
    MinLengthLogitsProcessor,
    MaxLengthCriteria,
    TopPLogitsWarper,
    TemperatureLogitsWarper,
)
from transformers.utils import ModelOutput
from transformers.generation import (
    GenerationMixin, 
    GreedySearchDecoderOnlyOutput,
    GreedySearchEncoderDecoderOutput,
    SampleEncoderDecoderOutput, 
    GenerationConfig,
)
from transformers.modeling_outputs import CausalLMOutputWithPast

@dataclass
class LogitSampleDecoderOnlyOutput(ModelOutput):
    sequences: torch.LongTensor = None
    scores: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    logits: Optional[Tuple[torch.FloatTensor]] = None
    reinforced_logits: Optional[Tuple[torch.FloatTensor]] = None

@dataclass
class LogitDecoderOnlyOutput(ModelOutput):
    sequences: torch.LongTensor = None
    scores: Optional[Tuple[torch.FloatTensor]] = None
    logits: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    past_key_values: Optional[Tuple[Tuple[Tuple[torch.FloatTensor]]]] = None
    reinforced_logits: Optional[Tuple[torch.FloatTensor]] = None

class WHPGenerationMixin(GenerationMixin):
    def __init__(self, basellm, reinforced_llm, alpha=1.) -> None:
        self.basellm = basellm
        self.reinforced_llm = reinforced_llm
        self.device = self.basellm.device
        self.config = self.basellm.config
        self.generation_config = basellm.generation_config
        self.alpha = alpha
    
    def prepare_input(self, input):
        input_ids = self.basellm.tokenizer(
            input, 
            return_tensors="pt",
        )['input_ids'].to(self.basellm.device)
        return input_ids
    
    def set_helpers(self, max_len, top_p, temperature):
        self.logits_processor = LogitsProcessorList([
            MinLengthLogitsProcessor(15, eos_token_id=self.basellm.generation_config.eos_token_id),
        ])
        self.logits_warper = LogitsProcessorList([
            TopPLogitsWarper(top_p),
            TemperatureLogitsWarper(temperature),
        ])
        self.stopping_criteria = StoppingCriteriaList([MaxLengthCriteria(max_length=max_len)])
    
    def can_generate(self):
        return True
    
    #! simplified from GenerationMixin:generate 
    def generate(
        self,
        inputs: Optional[torch.Tensor] = None,
        generation_config: Optional[GenerationConfig] = None,
        logits_processor: Optional[LogitsProcessorList] = None,
        stopping_criteria: Optional[StoppingCriteriaList] = None,
        prefix_allowed_tokens_fn: Optional[Callable[[int, torch.Tensor], List[int]]] = None,
        synced_gpus: Optional[bool] = None,
        streamer = None,
        negative_prompt_ids: Optional[torch.Tensor] = None,
        negative_prompt_attention_mask: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        # 1. Handle `generation_config` and kwargs that might update it, and validate the `.generate()` call
        # self._validate_model_class()

        # priority: `generation_config` argument > `model.generation_config` (the default generation config)
        if generation_config is None:
            # legacy: users may modify the model configuration to control generation. To trigger this legacy behavior,
            # three conditions must be met
            # 1) the generation config must have been created from the model config (`_from_model_config` field);
            # 2) the generation config must have seen no modification since its creation (the hash is the same);
            # 3) the user must have set generation parameters in the model config.
            if (
                self.generation_config._from_model_config
                and self.generation_config._original_object_hash == hash(self.generation_config)
                and self.config._has_non_default_generation_parameters()
            ):
                new_generation_config = GenerationConfig.from_model_config(self.config)
                if new_generation_config != self.generation_config:
                    warnings.warn(
                        "You have modified the pretrained model configuration to control generation. This is a"
                        " deprecated strategy to control generation and will be removed soon, in a future version."
                        " Please use and modify the model generation configuration (see"
                        " https://huggingface.co/docs/transformers/generation_strategies#default-text-generation-configuration )"
                    )
                    self.generation_config = new_generation_config
            generation_config = self.generation_config

        generation_config = copy.deepcopy(generation_config)
        model_kwargs = generation_config.update(**kwargs)  # All unused kwargs must be model kwargs
        self._validate_model_kwargs(model_kwargs.copy())

        logits_processor = logits_processor if logits_processor is not None else LogitsProcessorList()
        stopping_criteria = stopping_criteria if stopping_criteria is not None else StoppingCriteriaList()

        self.main_input_name = "input_ids"
        inputs_tensor, model_input_name, model_kwargs = self._prepare_model_inputs(
            inputs, generation_config.bos_token_id, model_kwargs
        )
 
        do_sample = kwargs.get('do_sample', False)
        max_len = kwargs.get('max_length', 200)
        top_p = kwargs.get('top_p', 1.0)
        temperature = kwargs.get('temperature', 1.0)

        if not do_sample:
            #! Greedy
            self.logits_processor = LogitsProcessorList([
                MinLengthLogitsProcessor(15, eos_token_id=self.tokenizer.eos_token_id),
            ])
            self.logits_processor = self._get_logits_processor(
                generation_config=generation_config, 
                input_ids_seq_length=5, #! a dummy value
                logits_processor=logits_processor,
                encoder_input_ids=inputs_tensor,
                prefix_allowed_tokens_fn=prefix_allowed_tokens_fn,
            )
            self.stopping_criteria = self._get_stopping_criteria(
                generation_config=generation_config, stopping_criteria=stopping_criteria,
            )
            outputs = self.greedy_search(
                input_ids=inputs_tensor,
                logits_processor=self.logits_processor,
                stopping_criteria=self.stopping_criteria,
                eos_token_id=kwargs.get('eod_token_id', None),
                pad_token_id=kwargs.get('pad_token_id', None),
                **model_kwargs,
            )
        else:
            #! Sampling
            self.logits_processor = LogitsProcessorList([
                MinLengthLogitsProcessor(
                    15, eos_token_id=self.basellm.generation_config.eos_token_id),
            ])
            self.logits_warper = LogitsProcessorList([
                TopPLogitsWarper(top_p),
                TemperatureLogitsWarper(temperature),
            ])
            self.stopping_criteria = self._get_stopping_criteria(
                generation_config=generation_config, stopping_criteria=stopping_criteria,
            )
            # self.stopping_criteria = StoppingCriteriaList([MaxLengthCriteria(max_length=max_len)])
            outputs = self.sample(
                # input_ids=inputs, 
                logits_processor=self.logits_processor,
                logits_warper=self.logits_warper,
                stopping_criteria=self.stopping_criteria,
                eos_token_id=kwargs.get('eod_token_id', None),
                pad_token_id=kwargs.get('pad_token_id', None),
                **model_kwargs,
            )
 
        return outputs
            
    def __call__(self, inputs: List[str], max_len=100, top_p=1.0, temperature=1e-9) -> List[str]:

        input_ids = self.basellm.tokenizer(
            inputs, 
            return_tensors="pt",
        )['input_ids'].to(self.basellm.device)

        outputs = self.generate(
            inputs=input_ids, max_length=max_len, 
            top_p=top_p, temperature=temperature,
            do_sample=(temperature < 1e-9),
        )
        outstrs = self.basellm.tokenizer.batch_decode(outputs, skip_special_tokens=True)
        genstrs = []
        for inp, out in zip(inputs, outstrs):
            genstrs.append(out.split(inp)[-1])
        return genstrs
        
    def prepare_inputs_for_generation(self, *args, **kwargs):
        return self.basellm.prepare_inputs_for_generation(*args, **kwargs)

    def prepare_inputs_for_generation_reinforced(self, *args, **kwargs):
        return self.reinforced_llm.prepare_inputs_for_generation(*args, **kwargs)
    
    #! adapted from GenerationMixin:greedy search; 
    @torch.no_grad()
    def greedy_search(
        self,
        input_ids: torch.LongTensor,
        logits_processor: Optional[LogitsProcessorList] = None,
        stopping_criteria: Optional[StoppingCriteriaList] = None,
        max_length: Optional[int] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[Union[int, List[int]]] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_scores: Optional[bool] = None,
        output_logits: Optional[bool] = None,
        return_dict_in_generate: Optional[bool] = None,
        synced_gpus: bool = False,
        streamer = None,
        **model_kwargs,
    ):
        # init values
        logits_processor = logits_processor if logits_processor is not None else LogitsProcessorList()
        stopping_criteria = stopping_criteria if stopping_criteria is not None else StoppingCriteriaList()
        pad_token_id = pad_token_id if pad_token_id is not None else self.generation_config.pad_token_id
        eos_token_id = eos_token_id if eos_token_id is not None else self.generation_config.eos_token_id
        if isinstance(eos_token_id, int):
            eos_token_id = [eos_token_id]
        eos_token_id_tensor = torch.tensor(eos_token_id).to(input_ids.device) if eos_token_id is not None else None
        output_scores = output_scores if output_scores is not None else self.generation_config.output_scores
        output_attentions = (
            output_attentions if output_attentions is not None else self.generation_config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.generation_config.output_hidden_states
        )
        return_dict_in_generate = (
            return_dict_in_generate
            if return_dict_in_generate is not None
            else self.generation_config.return_dict_in_generate
        )

        # init attention / hidden states / scores tuples
        raw_logits = () if (return_dict_in_generate and output_logits) else None
        reinforced_logits = () if (return_dict_in_generate and output_logits) else None
        scores = () if (return_dict_in_generate and output_scores) else None
        decoder_attentions = () if (return_dict_in_generate and output_attentions) else None
        cross_attentions = () if (return_dict_in_generate and output_attentions) else None
        decoder_hidden_states = () if (return_dict_in_generate and output_hidden_states) else None

        # if model is an encoder-decoder, retrieve encoder attention weights and hidden states
        if return_dict_in_generate and self.config.is_encoder_decoder:
            encoder_attentions = model_kwargs["encoder_outputs"].get("attentions") if output_attentions else None
            encoder_hidden_states = (
                model_kwargs["encoder_outputs"].get("hidden_states") if output_hidden_states else None
            )

        # keep track of which sequences are already finished
        unfinished_sequences = torch.ones(input_ids.shape[0], dtype=torch.long, device=input_ids.device)

        model_kwargs_reinforced = model_kwargs.get(
            "model_kwargs_reinforced", 
            copy.deepcopy(model_kwargs)
        )

        this_peer_finished = False  # used by synced_gpus only
        while True:
            if synced_gpus:
                # Under synced_gpus the `forward` call must continue until all gpus complete their sequence.
                # The following logic allows an early break if all peers finished generating their sequence
                this_peer_finished_flag = torch.tensor(0.0 if this_peer_finished else 1.0).to(input_ids.device)
                # send 0.0 if we finished, 1.0 otherwise
                dist.all_reduce(this_peer_finished_flag, op=dist.ReduceOp.SUM)
                # did all peers finish? the reduced sum will be 0.0 then
                if this_peer_finished_flag.item() == 0.0:
                    break

            ########################################################################################################
            #! Main logic
            ########################################################################################################

            model_inputs = self.prepare_inputs_for_generation(input_ids, **model_kwargs)
            # forward pass to get next token
            outputs = self.basellm(
                **model_inputs,
                return_dict=True,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
            )

            if input_ids.device != self.reinforced_llm.device:
                reinforced_input_ids = input_ids.to(self.reinforced_llm.device)
            else:
                reinforced_input_ids = input_ids.clone()
            reinforced_inputs = self.prepare_inputs_for_generation_reinforced(
                reinforced_input_ids, **model_kwargs_reinforced
            )
            for k, v in reinforced_inputs.items():
                if not v is None and isinstance(v, torch.Tensor) and v.device != self.reinforced_llm.device:
                    reinforced_inputs[k] = v.to(self.reinforced_llm.device)  #! move to device
            reinforced_outputs = self.reinforced_llm(
                **reinforced_inputs,
                return_dict=True,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
            )

            if synced_gpus and this_peer_finished:
                continue  # don't waste resources running the code we don't need

            next_token_logits = outputs.logits[:, -1, :]
            reinforced_next_token_logits = reinforced_outputs.logits[:, -1, :].to(next_token_logits.device) #? ensure they are on same device

            next_token_logits = next_token_logits.log_softmax(dim=-1)
            reinforced_next_token_logits = reinforced_next_token_logits.log_softmax(dim=-1)
            next_token_logits = next_token_logits - self.alpha * F.relu(reinforced_next_token_logits - next_token_logits)

            ########################################################################################################
            #! End of main logic
            ########################################################################################################

            # pre-process distribution
            next_tokens_scores = logits_processor(input_ids, next_token_logits)

            # Store scores, attentions and hidden_states when required
            if return_dict_in_generate:
                if output_scores:
                    scores += (next_tokens_scores,)
                if output_logits:
                    raw_logits += (next_token_logits,)
                    reinforced_logits += (reinforced_next_token_logits, )
                if output_attentions:
                    decoder_attentions += (
                        (outputs.decoder_attentions,) if self.config.is_encoder_decoder else (outputs.attentions,)
                    )
                    if self.config.is_encoder_decoder:
                        cross_attentions += (outputs.cross_attentions,)

                if output_hidden_states:
                    decoder_hidden_states += (
                        (outputs.decoder_hidden_states,)
                        if self.config.is_encoder_decoder
                        else (outputs.hidden_states,)
                    )

            # argmax
            next_tokens = torch.argmax(next_tokens_scores, dim=-1) 

            # finished sentences should have their next token be a padding token
            if eos_token_id is not None:
                if pad_token_id is None:
                    raise ValueError("If `eos_token_id` is defined, make sure that `pad_token_id` is defined.")
                next_tokens = next_tokens * unfinished_sequences + pad_token_id * (1 - unfinished_sequences)

            # update generated ids, model inputs, and length for next step
            input_ids = torch.cat([input_ids, next_tokens[:, None]], dim=-1)
            if streamer is not None:
                streamer.put(next_tokens.cpu())
            model_kwargs = self._update_model_kwargs_for_generation(
                outputs, model_kwargs, is_encoder_decoder=self.config.is_encoder_decoder
            )
            model_kwargs_reinforced = self._update_model_kwargs_for_generation(
                reinforced_outputs, model_kwargs_reinforced, is_encoder_decoder=self.config.is_encoder_decoder
            )

            # if eos_token was found in one sentence, set sentence to finished
            if eos_token_id_tensor is not None:
                unfinished_sequences = unfinished_sequences.mul(
                    next_tokens.tile(eos_token_id_tensor.shape[0], 1).ne(eos_token_id_tensor.unsqueeze(1)).prod(dim=0)
                )

                # stop when each sentence is finished
                if unfinished_sequences.max() == 0:
                    this_peer_finished = True

            unfinished_sequences = unfinished_sequences & ~stopping_criteria(input_ids, scores)
            this_peer_finished = unfinished_sequences.max() == 0
            if this_peer_finished and not synced_gpus:
                break

        if streamer is not None:
            streamer.end()

        if return_dict_in_generate:
            return LogitDecoderOnlyOutput(
                sequences=input_ids,
                scores=scores,
                logits=raw_logits,
                attentions=decoder_attentions,
                hidden_states=decoder_hidden_states,
                past_key_values=model_kwargs.get("past_key_values"),
                reinforced_logits=reinforced_logits,
            )
        else:
            return input_ids

    #! adapted from GenerationMixin:assited_decoding / sample; only support batch_size=1
    @torch.no_grad()
    def sample(
        self, 
        input_ids: torch.LongTensor,
        generation_config = None, 
        logits_processor: Optional[LogitsProcessorList] = None,
        logits_warper: Optional[LogitsProcessorList] = None,
        stopping_criteria: Optional[StoppingCriteriaList] = None,
        pad_token_id: Optional[int] = None,
        eos_token_id: Optional[Union[int, List[int]]] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_scores: Optional[bool] = None,
        output_logits: Optional[bool] = None,
        return_dict_in_generate: Optional[bool] = None,
        synced_gpus: bool = False,
        streamer = None,
        **model_kwargs,
    ):
        # priority: `generation_config` argument > `model.generation_config` (the default generation config)
        if generation_config is None:
            # legacy: users may modify the model configuration to control generation -- update the generation config
            # model attribute accordingly, if it was created from the model config
            if self.generation_config._from_model_config:
                new_generation_config = GenerationConfig.from_model_config(self.config)
                if new_generation_config != self.generation_config:
                    warnings.warn(
                        "You have modified the pretrained model configuration to control generation. This is a"
                        " deprecated strategy to control generation and will be removed soon, in a future version."
                        " Please use a generation configuration file (see"
                        " https://huggingface.co/docs/transformers/main_classes/text_generation )"
                    )
                    self.generation_config = new_generation_config
            generation_config = self.generation_config

        generation_config = copy.deepcopy(generation_config)

        # assert input_ids.shape[0] == 1, "batch_size must be 1"

        # init values
        logits_processor = logits_processor if logits_processor is not None else LogitsProcessorList()
        logits_warper = logits_warper if logits_warper is not None else LogitsProcessorList()
        stopping_criteria = stopping_criteria if stopping_criteria is not None else StoppingCriteriaList()
        pad_token_id = pad_token_id if pad_token_id is not None else self.generation_config.pad_token_id
        eos_token_id = eos_token_id if eos_token_id is not None else self.generation_config.eos_token_id
        if eos_token_id is not None and pad_token_id is None:
            raise ValueError("If `eos_token_id` is defined, make sure that `pad_token_id` is defined.")
        if isinstance(eos_token_id, int):
            eos_token_id = [eos_token_id]
        eos_token_id_tensor = torch.tensor(eos_token_id).to(input_ids.device) if eos_token_id is not None else None
        output_scores = output_scores if output_scores is not None else self.generation_config.output_scores
        output_attentions = (
            output_attentions if output_attentions is not None else self.generation_config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.generation_config.output_hidden_states
        )
        return_dict_in_generate = (
            return_dict_in_generate
            if return_dict_in_generate is not None
            else self.generation_config.return_dict_in_generate
        )

        # init attention / hidden states / scores tuples
        scores = () if (return_dict_in_generate and output_scores) else None
        logits = () if (return_dict_in_generate and output_logits) else None
        reinforced_logits = () if (return_dict_in_generate and output_logits) else None
        decoder_attentions = () if (return_dict_in_generate and output_attentions) else None
        cross_attentions = () if (return_dict_in_generate and output_attentions) else None
        decoder_hidden_states = () if (return_dict_in_generate and output_hidden_states) else None

        # if model is an encoder-decoder, retrieve encoder attention weights and hidden states
        if return_dict_in_generate and self.config.is_encoder_decoder:
            encoder_attentions = model_kwargs["encoder_outputs"].get("attentions") if output_attentions else None
            encoder_hidden_states = (
                model_kwargs["encoder_outputs"].get("hidden_states") if output_hidden_states else None
            )

        # keep track of which sequences are already finished
        unfinished_sequences = input_ids.new(input_ids.shape[0]).fill_(1)

        model_kwargs_reinforced = model_kwargs.get(
            "model_kwargs_reinforced", 
            copy.deepcopy(model_kwargs)
        )

        this_peer_finished = False  # used by synced_gpus only
        while True:
            if synced_gpus:
                # Under synced_gpus the `forward` call must continue until all gpus complete their sequence.
                # The following logic allows an early break if all peers finished generating their sequence
                this_peer_finished_flag = torch.tensor(0.0 if this_peer_finished else 1.0).to(input_ids.device)
                # send 0.0 if we finished, 1.0 otherwise
                dist.all_reduce(this_peer_finished_flag, op=dist.ReduceOp.SUM)
                # did all peers finish? the reduced sum will be 0.0 then
                if this_peer_finished_flag.item() == 0.0:
                    break
            
            ########################################################################################################
            #! Main logic
            ########################################################################################################

            model_inputs = self.prepare_inputs_for_generation(input_ids, **model_kwargs)
            # forward pass to get next token
            outputs = self.basellm(
                **model_inputs,
                return_dict=True,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
            )

            if input_ids.device != self.reinforced_llm.device:
                reinforced_input_ids = input_ids.to(self.reinforced_llm.device)
            else:
                reinforced_input_ids = input_ids.clone()
            reinforced_inputs = self.prepare_inputs_for_generation_reinforced(reinforced_input_ids, **model_kwargs_reinforced) 
            for k, v in reinforced_inputs.items():
                if not v is None and isinstance(v, torch.Tensor) and v.device != self.reinforced_llm.device:
                    reinforced_inputs[k] = v.to(self.reinforced_llm.device) #! move to device

            reinforced_outputs = self.reinforced_llm(
                **reinforced_inputs,
                return_dict=True,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
            )

            next_token_logits = outputs.logits[:, -1, :]
            reinforced_next_token_logits = reinforced_outputs.logits[:, -1, :].to(next_token_logits.device) #? ensure they are on same device

            next_token_logits = next_token_logits - self.alpha * F.relu(reinforced_next_token_logits - next_token_logits)
            
            # pre-process distribution
            next_token_scores = logits_processor(input_ids, next_token_logits)
            next_token_scores = logits_warper(input_ids, next_token_scores)

            ########################################################################################################
            #! End of main logic
            ########################################################################################################


            # Store scores, attentions and hidden_states when required
            if return_dict_in_generate:
                if output_scores:
                    scores += (next_token_scores,)
                if output_logits:
                    logits += (next_token_logits,)
                    reinforced_logits += (reinforced_next_token_logits, )
                if output_attentions:
                    decoder_attentions += (
                        (outputs.decoder_attentions,) if self.config.is_encoder_decoder else (outputs.attentions,)
                    )
                    if self.config.is_encoder_decoder:
                        cross_attentions += (outputs.cross_attentions,)

                if output_hidden_states:
                    decoder_hidden_states += (
                        (outputs.decoder_hidden_states,)
                        if self.config.is_encoder_decoder
                        else (outputs.hidden_states,)
                    )

            # sample
            probs = nn.functional.softmax(next_token_scores, dim=-1)
            next_tokens = torch.multinomial(probs, num_samples=1).squeeze(1)

            # finished sentences should have their next token be a padding token
            if eos_token_id is not None:
                if pad_token_id is None:
                    raise ValueError("If `eos_token_id` is defined, make sure that `pad_token_id` is defined.")
                next_tokens = next_tokens * unfinished_sequences + pad_token_id * (1 - unfinished_sequences)

            # update generated ids, model inputs, and length for next step
            input_ids = torch.cat([input_ids, next_tokens[:, None]], dim=-1)
            if streamer is not None:
                streamer.put(next_tokens.cpu())
            model_kwargs = self._update_model_kwargs_for_generation(
                outputs, model_kwargs, is_encoder_decoder=self.config.is_encoder_decoder
            )
            model_kwargs_reinforced = self._update_model_kwargs_for_generation(
                reinforced_outputs, model_kwargs_reinforced, is_encoder_decoder=self.config.is_encoder_decoder
            )

            # if eos_token was found in one sentence, set sentence to finished
            if eos_token_id_tensor is not None:
                unfinished_sequences = unfinished_sequences.mul(
                    next_tokens.tile(eos_token_id_tensor.shape[0], 1).ne(eos_token_id_tensor.unsqueeze(1)).prod(dim=0)
                )

                # stop when each sentence is finished
                if unfinished_sequences.max() == 0:
                    this_peer_finished = True

            # stop if we exceed the maximum length
            if stopping_criteria(input_ids, scores):
                this_peer_finished = True

            if this_peer_finished and not synced_gpus:
                break
        
        if streamer is not None:
            streamer.end()

        if return_dict_in_generate:
            if self.config.is_encoder_decoder:
                #! Not implemented
                return SampleEncoderDecoderOutput(
                    sequences=input_ids,
                    scores=scores,
                    encoder_attentions=encoder_attentions,
                    encoder_hidden_states=encoder_hidden_states,
                    decoder_attentions=decoder_attentions,
                    cross_attentions=cross_attentions,
                    decoder_hidden_states=decoder_hidden_states,
                )
            else:
                return LogitSampleDecoderOnlyOutput(
                    sequences=input_ids,
                    scores=scores,
                    attentions=decoder_attentions,
                    hidden_states=decoder_hidden_states,
                    logits=logits,
                    reinforced_logits=reinforced_logits,
                )
        else:
            return input_ids
    


class WHPLLM(torch.nn.Module, WHPGenerationMixin):
    def __init__(self, basellm : AutoModelForCausalLM, reinforced_llm : AutoModelForCausalLM, alpha=1., config=None, tokenizer=None, **kwargs):
        super().__init__()
        self.basellm = basellm
        self.reinforced_llm = reinforced_llm
        if config is None:
            self.config = self.basellm.config
        self.alpha = alpha
        self.device = self.basellm.device
        self.generation_config = basellm.generation_config
        self.tokenizer = tokenizer

    def forward(self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple]:
        
        output_attentions = False
        output_hidden_states = False
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        v_b = self.basellm(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=True
        )
        
        v_r = self.reinforced_llm(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=True
        )
        
        logits = v_b.logits - self.alpha * F.relu(v_r.logits - v_b.logits)
        
        if not return_dict:
            return (logits,) + v_b[1:]

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = nn.CrossEntropyLoss()
            shift_logits = shift_logits.view(-1, self.config.vocab_size)
            shift_labels = shift_labels.view(-1)
            # Enable model parallelism
            shift_labels = shift_labels.to(shift_logits.device)
            loss = loss_fct(shift_logits, shift_labels)

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=None,
            hidden_states=None,
            attentions=None,
        )