import argparse
import os
import time

import torch
from torch.profiler import ProfilerActivity, profile, record_function
from transformers import LlamaForCausalLM, LlamaTokenizer

import colossalai
from colossalai.inference.tensor_parallel.engine import TPInferEngine
from colossalai.logging import disable_existing_loggers
from colossalai.shardformer import ShardConfig
from colossalai.testing import clear_cache_before_run, rerun_if_address_is_in_use, spawn

os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "true"


def print_perf_stats(latency_set, config, bs, warmup=3):
    # trim warmup queries
    latency_set = list(latency_set)
    latency_set = latency_set[warmup:]
    count = len(latency_set)

    if count > 0:
        latency_set.sort()
        avg = sum(latency_set) / count
        num_layers = getattr(config, "num_layers", config.num_hidden_layers)
        num_parameters = num_layers * config.hidden_size * config.hidden_size * 12
        num_bytes = 2

        print("Avg Per Token Latency: {0:8.2f} ms".format(avg * 1000))
        print("Avg BW: {0:8.2f} GB/s".format(1 / avg * num_parameters * num_bytes / 1e9))
        print("Avg flops: {0:8.2f} TFlops/s".format(1 / avg * num_parameters * num_bytes * bs / 1e12))


def run_llama_test(args):
    llama_model_path = args.path
    max_batch_size = args.batch_size
    max_input_len = args.input_len
    max_output_len = args.output_len

    tokenizer = LlamaTokenizer.from_pretrained(llama_model_path)
    tokenizer.pad_token_id = tokenizer.unk_token_id
    model = LlamaForCausalLM.from_pretrained(llama_model_path, pad_token_id=tokenizer.eos_token_id)
    model = model.half()
    model_config = model.config

    shard_config = ShardConfig(enable_tensor_parallelism=True if args.tp_size > 1 else False, inference_only=True)
    infer_engine = TPInferEngine(model, shard_config, max_batch_size, max_input_len, max_output_len)

    generate_kwargs = dict(max_new_tokens=max_output_len, do_sample=False)
    input_tokens = {
        "input_ids": torch.randint(1, 1000, (max_batch_size, max_input_len), device="cuda"),
        "attention_mask": torch.ones((max_batch_size, max_input_len), device="cuda"),
    }

    iters = 10
    times = []

    for i in range(iters):
        torch.cuda.synchronize()
        start = time.time()
        outputs = infer_engine.generate(input_tokens, **generate_kwargs)
        torch.cuda.synchronize()
        end = time.time()
        out_len = outputs.shape[1]
        print("generation time {} s".format(str(end - start)))
        times.append((end - start) / (out_len - max_input_len))

    print("outputs, ", len(outputs))
    print_perf_stats(times, model_config, max_batch_size)

    with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA], record_shapes=True) as prof:
        with record_function("model_inference"):
            torch.cuda.synchronize()
            outputs = infer_engine.generate(input_tokens, **generate_kwargs)
            torch.cuda.synchronize()
    print(prof.key_averages().table(sort_by="cuda_time_total", row_limit=10))


def check_llama(rank, world_size, port, args):
    disable_existing_loggers()
    colossalai.launch(config={}, rank=rank, world_size=world_size, host="localhost", port=port, backend="nccl")
    run_llama_test(args)


@rerun_if_address_is_in_use()
@clear_cache_before_run()
def test_llama(args):
    spawn(check_llama, args.tp_size, args=args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--path", type=str, help="Model path", required=True)
    parser.add_argument("-tp", "--tp_size", type=int, default=1, help="Tensor parallel size")
    parser.add_argument("-b", "--batch_size", type=int, default=16, help="Maximum batch size")
    parser.add_argument("--input_len", type=int, default=1024, help="Maximum input length")
    parser.add_argument("--output_len", type=int, default=128, help="Maximum output length")

    args = parser.parse_args()

    test_llama(args)
