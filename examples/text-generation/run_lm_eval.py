# coding=utf-8
# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

###############################################################################
# Copyright (C) 2020-2025 Habana Labs, Ltd. an Intel Company
###############################################################################

import argparse
import json
import logging
import multiprocessing as mp
import os
import time

import lm_eval.evaluator
import lm_eval.tasks
import psutil
import torch
import torch.nn.functional as F

# Local imports
from run_generation import setup_parser
from utils import finalize_quantization, initialize_model, save_model

from optimum.habana.utils import get_hpu_memory_stats


os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
logger = logging.getLogger(__name__)


# This hack is a workaround to limitations of lm_eval which always allocates
# mp.Pool with max cpu count which explodes on multinode scenarios and for hpu
# create multiprocess with spawn context
OrigPool = mp.Pool


def LimitedSpawnPool(_):
    spawn_context = mp.get_context("spawn")
    physical_cpu_count = psutil.cpu_count(logical=False)
    pool_size = physical_cpu_count
    world_size = int(os.getenv("WORLD_SIZE", 1))
    if world_size == 0:
        world_size = 1
    pool_size //= world_size
    if (pool_size * world_size) != physical_cpu_count:
        pool_size -= 1
    return spawn_context.Pool(pool_size)


mp.Pool = LimitedSpawnPool


def setup_lm_eval_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter, description="Evaluation script for HPU"
    )
    parser.add_argument(
        "--buckets",
        type=int,
        nargs="+",
        help="Input length buckets to use with static_shapes",
        default=[16, 32, 64, 128, 189, 284, 384],
    )

    parser.add_argument(
        "--output_file", "-o", type=str, help="Output file with end results and runtime parameters", required=True
    )
    parser.add_argument(
        "--tasks",
        type=str,
        nargs="+",
        help="Tasks to run",
        default=["hellaswag", "lambada_openai", "piqa", "winogrande"],
    )
    parser.add_argument("--limit_iters", type=int, help="limit examples to run that many iterations", default=None)
    parser.add_argument(
        "--show_config",
        action="store_true",
        default=False,
        help="If True, shows the the full config of all tasks at the end of the evaluation.",
    )
    parser.add_argument("--max_graphs", type=int, help="Maximum number of HPU graphs", default=None)
    args = setup_parser(parser)

    return args


def main():
    args = setup_lm_eval_parser()
    model, _, tokenizer, generation_config = initialize_model(args, logger)

    if args.trust_remote_code:
        # trust_remote_code fix was introduced in lm_eval 0.4.3
        # https://github.com/EleutherAI/lm-evaluation-harness/pull/1998/files
        # We need to cherry-pick the fix manually untill we upgrade (SW-190418)
        import datasets

        datasets.config.HF_DATASETS_TRUST_REMOTE_CODE = True

    eval_start = time.perf_counter()
    with torch.no_grad():
        from neural_compressor.evaluation.lm_eval import evaluate, LMEvalParser
        eval_args = LMEvalParser(
            model="hf", 
            user_model=model,
            tokenizer=tokenizer,
            batch_size=1,
            tasks=','.join(args.tasks),
            device="hpu",
            pad_to_buckets=True,
            num_fewshot=5 if ("mmlu" in args.tasks) else 0,
        )
        results = evaluate(eval_args)
    if args.device == "hpu":
        import habana_frameworks.torch.hpu as torch_hpu

        torch_hpu.synchronize()
    eval_end = time.perf_counter()

    results["args"] = vars(args)
    results["duration"] = eval_end - eval_start

    if args.local_rank == 0:
        if args.device == "hpu":
            mem = get_hpu_memory_stats()
            for k, v in mem.items():
                print("{:35} = {} GB".format(k[:-5].replace("_", " ").capitalize(), v))
        json.dump(results, open(args.output_file, "w"), indent=2)
        if args.show_config:
            print(json.dumps(results, indent=2))
    if args.quant_config:
        finalize_quantization(model)
    if args.save_quantized_model_with_inc:
        save_model(model, tokenizer, args.saved_model_path)

    if args.const_serialization_path and os.path.isdir(args.const_serialization_path):
        import shutil

        shutil.rmtree(args.const_serialization_path)


if __name__ == "__main__":
    main()
