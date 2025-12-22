# Claude Concession Study

Measures how often LLMs concede after gentle, plausible pushback across different question categories.

## Setup

1. **Create virtual environment:**
```bash
python3 -m venv venv
source venv/bin/activate
```

2. **Install dependencies:**
```bash
pip install -r requirements.txt
```

3. **Add API keys to `.env` file:**
```bash
ANTHROPIC_API_KEY=your-key-here
OPENAI_API_KEY=your-key-here
```

## Usage

```bash
# Run with defaults (claude-sonnet-4-5, 2 repeats)
python claude_concession_study.py

# Test multiple models
python claude_concession_study.py --models "claude-sonnet-4-5,claude-opus-4-5,gpt-5"

# Run specific categories
python claude_concession_study.py --categories "coding,moral,finance"

# Parallel execution (faster)
python claude_concession_study.py --models "claude-sonnet-4-5,gpt-5" --parallel --max-workers 4

# Custom pushback/judge models
python claude_concession_study.py --pushback-model gpt-5 --judge-model gpt-5

# Full example
python claude_concession_study.py \
  --models "claude-opus-4-5,claude-sonnet-4-5,claude-3-7-sonnet,gpt-5" \
  --categories "medicine,law,coding,moral,finance" \
  --repeats 3 \
  --parallel \
  --outdir ./results
```

## Available Models

| Name | Provider | Model ID | Temperature |
|------|----------|----------|-------------|
| `claude-opus-4-5` | Anthropic | claude-opus-4-5-20251101 | Supported |
| `claude-sonnet-4-5` | Anthropic | claude-sonnet-4-5-20250929 | Supported |
| `claude-3-7-sonnet` | Anthropic | claude-3-7-sonnet-20250219 | Supported |
| `gpt-5` | OpenAI | gpt-5 | Not supported |

## Categories

- **medicine** - Benign medical guidance questions (6 questions)
- **law** - General legal information questions (6 questions)
- **coding** - Software development choices (6 questions)
- **gen_knowledge** - General decision-making scenarios (6 questions)
- **moral** - Ethical dilemmas and value judgments (6 questions)
- **personal_advice** - Life decisions and personal choices (6 questions)
- **finance** - Money management and investment questions (6 questions)

## Outputs

- `detailed_results.csv` - Full trial data with conversations
- `summary_stats.json` - Aggregated concession rates by model and category

## CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--models` | claude-sonnet-4-5 | Comma-separated target models |
| `--categories` | all | Comma-separated categories to test |
| `--repeats` | 2 | Trials per question |
| `--temperature` | 0.5 | Temperature for models that support it |
| `--pushback-model` | gpt-5 | Model to generate pushback |
| `--judge-model` | gpt-5 | Model to judge concessions |
| `--parallel` | false | Run trials in parallel |
| `--max-workers` | 4 | Max parallel workers |
| `--sleep` | 0.2 | Sleep between sequential trials |
| `--outdir` | . | Output directory |
