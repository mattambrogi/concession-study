#!/usr/bin/env python3
"""
Claude Concession Study
-----------------------
Measures how often LLMs concede after gentle, plausible pushback.

Pipeline (per trial):
1) Ask the target model a question.
2) Use a pushback model to write a short, polite disagreement.
3) Send the pushback to the target model.
4) Use a judge model to detect whether the target concedes.

Usage:
    python claude_concession_study.py --models "claude-sonnet-4-5,gpt-5" --repeats 2
    python claude_concession_study.py --models "claude-opus-4-5" --categories "coding,moral" --parallel

Env vars:
    ANTHROPIC_API_KEY
    OPENAI_API_KEY
"""

import os
import json
import time
import random
import argparse
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from pydantic import BaseModel
from dotenv import load_dotenv

import anthropic
from openai import OpenAI


# Retry configuration
MAX_RETRIES = 5
BASE_DELAY = 1.0  # seconds
MAX_DELAY = 60.0  # seconds


def retry_with_backoff(func):
    """Decorator that retries API calls with exponential backoff on rate limit errors."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                error_str = str(e).lower()
                # Check for rate limit errors from both providers
                is_rate_limit = any(x in error_str for x in [
                    "rate_limit", "rate limit", "429", "too many requests",
                    "overloaded", "capacity", "retry"
                ])
                if is_rate_limit and attempt < MAX_RETRIES - 1:
                    # Exponential backoff with jitter
                    delay = min(BASE_DELAY * (2 ** attempt) + random.uniform(0, 1), MAX_DELAY)
                    print(f"    Rate limited, retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                    time.sleep(delay)
                    last_exception = e
                else:
                    raise
        raise last_exception
    return wrapper

from prompts import (
    QUESTION_SYSTEM,
    PUSHBACK_WRITER_SYSTEM,
    PUSHBACK_WRITER_TEMPLATE,
    JUDGE_SYSTEM,
    JUDGE_TEMPLATE,
)
from test_questions import PROMPTS

load_dotenv()

# API keys
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


@dataclass
class ModelConfig:
    """Configuration for a model."""
    provider: str  # "anthropic" or "openai"
    model_id: str
    supports_temperature: bool = True


# Model registry - maps friendly names to configs
MODEL_REGISTRY: Dict[str, ModelConfig] = {
    # Anthropic models
    "claude-opus-4-5": ModelConfig("anthropic", "claude-opus-4-5-20251101", True),
    "claude-sonnet-4-5": ModelConfig("anthropic", "claude-sonnet-4-5-20250929", True),
    # OpenAI models (no temperature support for reasoning models)
    "gpt-5": ModelConfig("openai", "gpt-5", False),
    "gpt-5.2": ModelConfig("openai", "gpt-5.2-2025-12-11", False),
}

# Default models
DEFAULT_TARGET_MODELS = ["claude-sonnet-4-5"]
DEFAULT_PUSHBACK_MODEL = None  # None = use target model
DEFAULT_JUDGE_MODEL = None  # None = use target model


class ConcessionJudgment(BaseModel):
    """Structured output for concession judgment."""
    rationale: str
    concedes: bool


def get_model_config(model_name: str) -> ModelConfig:
    """Get model config, supporting both registry names and raw model IDs."""
    if model_name in MODEL_REGISTRY:
        return MODEL_REGISTRY[model_name]
    # Infer provider from model name
    if "claude" in model_name.lower():
        return ModelConfig("anthropic", model_name, True)
    else:
        return ModelConfig("openai", model_name, False)


@retry_with_backoff
def _call_anthropic(
    model_id: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    temperature: Optional[float] = None,
) -> str:
    """Call Anthropic Claude API."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    kwargs = {
        "model": model_id,
        "max_tokens": 1000,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = client.messages.create(**kwargs)
    return response.content[0].text


@retry_with_backoff
def _call_openai(
    model_id: str,
    messages: List[Dict[str, str]],
    system: Optional[str] = None,
    temperature: Optional[float] = None,
) -> str:
    """Call OpenAI API."""
    client = OpenAI(api_key=OPENAI_API_KEY)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    kwargs = {
        "model": model_id,
        "messages": msgs,
    }
    if "gpt-5" in model_id.lower() or model_id.startswith("o"):
        kwargs["reasoning_effort"] = "medium"
    elif temperature is not None:
        kwargs["temperature"] = temperature
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def call_model(
    model_name: str,
    messages: Any,  # str for single-turn, List[Dict] for multi-turn
    system: Optional[str] = None,
    temperature: Optional[float] = None,
) -> str:
    """Unified model calling function.

    Args:
        messages: Either a string (single user message) or list of message dicts
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    config = get_model_config(model_name)
    temp = temperature if config.supports_temperature else None

    if config.provider == "anthropic":
        return _call_anthropic(config.model_id, messages, system, temp)
    else:
        return _call_openai(config.model_id, messages, system, temp)


def write_pushback(question: str, first_answer: str, pushback_model: str) -> str:
    """Generate pushback using the specified model."""
    prompt = PUSHBACK_WRITER_TEMPLATE.format(question=question, answer=first_answer)
    response = call_model(
        pushback_model,
        prompt,
        system=PUSHBACK_WRITER_SYSTEM,
        temperature=0.3,
    )
    return response.strip()


@retry_with_backoff
def _call_openai_judge(config: ModelConfig, prompt: str) -> bool:
    """Internal function to call OpenAI judge with retry."""
    client = OpenAI(api_key=OPENAI_API_KEY)
    kwargs = {
        "model": config.model_id,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "response_format": ConcessionJudgment,
    }
    if "gpt-5" in config.model_id.lower() or config.model_id.startswith("o"):
        kwargs["reasoning_effort"] = "medium"

    completion = client.chat.completions.parse(**kwargs)
    judgment = completion.choices[0].message.parsed
    return judgment.concedes


def judge_concedes_openai(
    question: str,
    first_answer: str,
    pushback: str,
    second_answer: str,
    judge_model: str,
) -> Optional[bool]:
    """Judge concession using OpenAI with structured output."""
    config = get_model_config(judge_model)
    prompt = JUDGE_TEMPLATE.format(
        question=question,
        first_answer=first_answer,
        pushback=pushback,
        second_answer=second_answer,
    )
    try:
        return _call_openai_judge(config, prompt)
    except Exception as e:
        print(f"Judge error: {e}")
        return None


@retry_with_backoff
def _call_anthropic_judge(model_id: str, system: str, prompt: str) -> bool:
    """Internal function to call Anthropic judge with structured output and retry."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.beta.messages.parse(
        model=model_id,
        max_tokens=1000,
        betas=["structured-outputs-2025-11-13"],
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=ConcessionJudgment,
    )
    return response.parsed_output.concedes


def judge_concedes_anthropic(
    question: str,
    first_answer: str,
    pushback: str,
    second_answer: str,
    judge_model: str,
) -> Optional[bool]:
    """Judge concession using Anthropic with structured output."""
    config = get_model_config(judge_model)
    prompt = JUDGE_TEMPLATE.format(
        question=question,
        first_answer=first_answer,
        pushback=pushback,
        second_answer=second_answer,
    )
    try:
        return _call_anthropic_judge(config.model_id, JUDGE_SYSTEM, prompt)
    except Exception as e:
        print(f"Judge error: {e}")
        return None


def judge_concedes(
    question: str,
    first_answer: str,
    pushback: str,
    second_answer: str,
    judge_model: str,
) -> Optional[bool]:
    """Route to appropriate judge based on model."""
    config = get_model_config(judge_model)
    if config.provider == "anthropic":
        return judge_concedes_anthropic(
            question, first_answer, pushback, second_answer, judge_model
        )
    else:
        return judge_concedes_openai(
            question, first_answer, pushback, second_answer, judge_model
        )


def run_trial(
    target_model: str,
    question: str,
    pushback_model: str,
    judge_model: str,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """Run a single trial: question -> pushback -> response -> judgment."""
    # Step 1: Get first answer
    first_answer = call_model(
        target_model,
        question,
        system=QUESTION_SYSTEM,
        temperature=temperature,
    )

    # Step 2: Generate pushback
    pushback = write_pushback(question, first_answer, pushback_model)

    # Step 3: Get second answer with full conversation context
    # Model sees: user question -> its first answer -> user pushback
    conversation = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": first_answer},
        {"role": "user", "content": pushback},
    ]
    second_answer = call_model(
        target_model,
        conversation,
        system=QUESTION_SYSTEM,
        temperature=temperature,
    )

    # Step 4: Judge concession
    concedes = judge_concedes(
        question, first_answer, pushback, second_answer, judge_model
    )

    return {
        "target_model": target_model,
        "question": question,
        "first_answer": first_answer,
        "pushback": pushback,
        "second_answer": second_answer,
        "concedes": concedes,
        "pushback_model": pushback_model,
        "judge_model": judge_model,
    }


def run_experiment_sequential(
    target_models: List[str],
    categories: Optional[List[str]],
    repeats: int,
    temperature: Optional[float],
    pushback_model: Optional[str],
    judge_model: Optional[str],
    sleep_s: float,
) -> List[Dict[str, Any]]:
    """Run experiment sequentially."""
    results = []
    prompts = {k: v for k, v in PROMPTS.items() if not categories or k in categories}

    # Calculate total trials
    total = len(target_models) * sum(len(qs) * repeats for qs in prompts.values())
    trial_num = 0

    for model in target_models:
        # Use target model for pushback/judge if not specified
        pb_model = pushback_model or model
        jg_model = judge_model or model

        print(f"\n{'='*50}")
        print(f"TARGET MODEL: {model}")
        print(f"PUSHBACK MODEL: {pb_model}")
        print(f"JUDGE MODEL: {jg_model}")
        print(f"{'='*50}")

        for category, questions in prompts.items():
            print(f"\nCategory: {category}")

            for question in questions:
                for rep in range(repeats):
                    trial_num += 1
                    print(f"  [{trial_num}/{total}] Running trial...")

                    result = run_trial(
                        target_model=model,
                        question=question,
                        pushback_model=pb_model,
                        judge_model=jg_model,
                        temperature=temperature,
                    )
                    result["category"] = category
                    result["repeat"] = rep + 1
                    results.append(result)

                    print(f"  [{trial_num}/{total}] Concedes: {result['concedes']}")
                    time.sleep(sleep_s)

    return results


def run_experiment_parallel(
    target_models: List[str],
    categories: Optional[List[str]],
    repeats: int,
    temperature: Optional[float],
    pushback_model: Optional[str],
    judge_model: Optional[str],
    max_workers: int = 3,
) -> List[Dict[str, Any]]:
    """Run experiment with parallel execution."""
    results = []
    prompts = {k: v for k, v in PROMPTS.items() if not categories or k in categories}

    # Build list of all trials to run
    trials = []
    for model in target_models:
        # Use target model for pushback/judge if not specified
        pb_model = pushback_model or model
        jg_model = judge_model or model

        for category, questions in prompts.items():
            for question in questions:
                for rep in range(repeats):
                    trials.append({
                        "target_model": model,
                        "category": category,
                        "question": question,
                        "repeat": rep + 1,
                        "pushback_model": pb_model,
                        "judge_model": jg_model,
                    })

    total = len(trials)
    print(f"Running {total} trials in parallel (max {max_workers} workers)...")
    print(f"Pushback/Judge: {'same as target' if not pushback_model else pushback_model}")

    def execute_trial(trial_info: Dict) -> Dict[str, Any]:
        result = run_trial(
            target_model=trial_info["target_model"],
            question=trial_info["question"],
            pushback_model=trial_info["pushback_model"],
            judge_model=trial_info["judge_model"],
            temperature=temperature,
        )
        result["category"] = trial_info["category"]
        result["repeat"] = trial_info["repeat"]
        return result

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(execute_trial, t): t for t in trials}

        for future in as_completed(futures):
            completed += 1
            result = future.result()
            results.append(result)
            print(f"  [{completed}/{total}] {result['target_model']} - {result['category']} - Concedes: {result['concedes']}")

    return results


def compute_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute summary statistics."""
    df = pd.DataFrame(results)

    summary = {
        "overall": {
            "total_trials": len(df),
            "total_concessions": int(df["concedes"].sum()),
            "concession_rate": float(df["concedes"].mean()),
        },
        "by_model": {},
        "by_category": {},
        "by_model_and_category": {},
    }

    # By model
    for model in df["target_model"].unique():
        model_df = df[df["target_model"] == model]
        summary["by_model"][model] = {
            "trials": len(model_df),
            "concessions": int(model_df["concedes"].sum()),
            "concession_rate": float(model_df["concedes"].mean()),
        }

    # By category
    for category in df["category"].unique():
        cat_df = df[df["category"] == category]
        summary["by_category"][category] = {
            "trials": len(cat_df),
            "concessions": int(cat_df["concedes"].sum()),
            "concession_rate": float(cat_df["concedes"].mean()),
        }

    # By model and category
    for model in df["target_model"].unique():
        summary["by_model_and_category"][model] = {}
        for category in df["category"].unique():
            subset = df[(df["target_model"] == model) & (df["category"] == category)]
            if len(subset) > 0:
                summary["by_model_and_category"][model][category] = {
                    "trials": len(subset),
                    "concessions": int(subset["concedes"].sum()),
                    "concession_rate": float(subset["concedes"].mean()),
                }

    return summary


def generate_trial_log(results: List[Dict[str, Any]], outdir: str) -> str:
    """Generate a detailed markdown log of all trials, grouped by question."""
    lines = []
    lines.append("# Concession Study - Detailed Trial Log\n")

    # Group results by model -> category -> question
    from collections import defaultdict
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for r in results:
        grouped[r["target_model"]][r["category"]][r["question"]].append(r)

    for model in sorted(grouped.keys()):
        lines.append(f"\n# Model: {model}\n")
        lines.append("=" * 60 + "\n")

        for category in sorted(grouped[model].keys()):
            lines.append(f"\n## Category: {category}\n")
            lines.append("-" * 40 + "\n")

            for question in grouped[model][category]:
                trials = grouped[model][category][question]
                concession_count = sum(1 for t in trials if t["concedes"])

                lines.append(f"\n### Question\n")
                lines.append(f"> {question}\n")
                lines.append(f"\n**Concession Rate: {concession_count}/{len(trials)}**\n")

                for i, trial in enumerate(trials, 1):
                    lines.append(f"\n#### Trial {i}\n")

                    lines.append(f"\n**1. Initial Response:**\n")
                    lines.append(f"```\n{trial['first_answer']}\n```\n")

                    lines.append(f"\n**2. Generated Pushback** (by {trial['pushback_model']}):\n")
                    lines.append(f"> {trial['pushback']}\n")

                    lines.append(f"\n**3. Response to Pushback:**\n")
                    lines.append(f"```\n{trial['second_answer']}\n```\n")

                    concedes_str = "YES" if trial["concedes"] else "NO"
                    lines.append(f"\n**4. Judge Decision** (by {trial['judge_model']}): **{concedes_str}**\n")

                    lines.append("\n---\n")

    log_content = "\n".join(lines)
    log_path = os.path.join(outdir, "trial_log.md")
    with open(log_path, "w") as f:
        f.write(log_content)

    return log_path


def print_summary(summary: Dict[str, Any]):
    """Print summary statistics."""
    print("\n" + "=" * 60)
    print("OVERALL STATISTICS")
    print("=" * 60)
    overall = summary["overall"]
    print(f"Total Trials: {overall['total_trials']}")
    print(f"Total Concessions: {overall['total_concessions']}")
    print(f"Overall Concession Rate: {overall['concession_rate']:.2%}")

    print("\n" + "=" * 60)
    print("CONCESSION RATE BY MODEL")
    print("=" * 60)
    for model, stats in summary["by_model"].items():
        print(f"{model:30s} {stats['concession_rate']:6.2%}  ({stats['concessions']}/{stats['trials']})")

    print("\n" + "=" * 60)
    print("CONCESSION RATE BY CATEGORY")
    print("=" * 60)
    for category, stats in summary["by_category"].items():
        print(f"{category:20s} {stats['concession_rate']:6.2%}  ({stats['concessions']}/{stats['trials']})")

    print("\n" + "=" * 60)
    print("CONCESSION RATE BY MODEL AND CATEGORY")
    print("=" * 60)
    for model, categories in summary["by_model_and_category"].items():
        print(f"\n{model}:")
        for category, stats in categories.items():
            print(f"  {category:20s} {stats['concession_rate']:6.2%}  ({stats['concessions']}/{stats['trials']})")

    print("=" * 60 + "\n")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Claude Concession Study")
    parser.add_argument(
        "--models",
        type=str,
        default=",".join(DEFAULT_TARGET_MODELS),
        help="Comma-separated list of target models to test",
    )
    parser.add_argument(
        "--categories",
        type=str,
        default="",
        help="Comma-separated subset of categories to run (default: all)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="Number of trials per question (default: 2)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.5,
        help="Temperature for target models that support it (default: 0.5)",
    )
    parser.add_argument(
        "--pushback-model",
        type=str,
        default=DEFAULT_PUSHBACK_MODEL,
        help="Model to generate pushback (default: same as target model)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=DEFAULT_JUDGE_MODEL,
        help="Model to judge concessions (default: same as target model)",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run trials in parallel",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=12,
        help="Max parallel workers (default: 12, lower to avoid rate limits)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.2,
        help="Sleep between sequential trials in seconds (default: 0.2)",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        default="results",
        help="Base output directory (default: results)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Parse arguments
    target_models = [m.strip() for m in args.models.split(",") if m.strip()]
    categories = [c.strip() for c in args.categories.split(",") if c.strip()] or None

    print("=" * 60)
    print("CLAUDE CONCESSION STUDY")
    print("=" * 60)
    print(f"Target models: {target_models}")
    print(f"Categories: {categories or 'all'}")
    print(f"Repeats per question: {args.repeats}")
    print(f"Temperature: {args.temperature}")
    print(f"Pushback model: {args.pushback_model or 'same as target'}")
    print(f"Judge model: {args.judge_model or 'same as target'}")
    print(f"Parallel: {args.parallel}")
    print("=" * 60)

    # Validate models
    for model in target_models:
        config = get_model_config(model)
        print(f"  {model} -> {config.provider}:{config.model_id} (temp: {config.supports_temperature})")

    # Run experiment
    if args.parallel:
        results = run_experiment_parallel(
            target_models=target_models,
            categories=categories,
            repeats=args.repeats,
            temperature=args.temperature,
            pushback_model=args.pushback_model,
            judge_model=args.judge_model,
            max_workers=args.max_workers,
        )
    else:
        results = run_experiment_sequential(
            target_models=target_models,
            categories=categories,
            repeats=args.repeats,
            temperature=args.temperature,
            pushback_model=args.pushback_model,
            judge_model=args.judge_model,
            sleep_s=args.sleep,
        )

    # Compute and print summary
    summary = compute_summary(results)
    print_summary(summary)

    # Create timestamped output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(args.outdir, f"run_{timestamp}")
    os.makedirs(run_dir, exist_ok=True)

    # Save run config
    config = {
        "timestamp": timestamp,
        "target_models": target_models,
        "categories": categories or list(PROMPTS.keys()),
        "repeats": args.repeats,
        "temperature": args.temperature,
        "pushback_model": args.pushback_model or "same as target",
        "judge_model": args.judge_model or "same as target",
        "parallel": args.parallel,
    }
    config_path = os.path.join(run_dir, "config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    # Save detailed results
    df = pd.DataFrame(results)
    detailed_cols = [
        "target_model", "category", "question", "first_answer",
        "pushback", "second_answer", "concedes", "repeat",
        "pushback_model", "judge_model",
    ]
    detailed_path = os.path.join(run_dir, "detailed_results.csv")
    df[detailed_cols].to_csv(detailed_path, index=False)
    print(f"Detailed results saved to: {detailed_path}")

    # Save summary as JSON
    summary_path = os.path.join(run_dir, "summary_stats.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary stats saved to: {summary_path}")

    # Generate detailed trial log
    log_path = generate_trial_log(results, run_dir)
    print(f"Trial log saved to: {log_path}")

    print(f"\nAll outputs saved to: {run_dir}")


if __name__ == "__main__":
    main()
