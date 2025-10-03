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
# Load environment variables
source .env

# Run with defaults (2 repeats, temp 0.5)
python claude_concession_study.py

# Custom settings
python claude_concession_study.py --repeats 5 --temperature 0.7

# Run specific categories only
python claude_concession_study.py --categories "coding,gen_knowledge"

# Output to custom directory
python claude_concession_study.py --outdir ./results
```

## Outputs

- `claude_concession_results.csv` - Detailed trial data
- `claude_concession_summary.csv` - Aggregated concession rates by category
- Bar chart visualization (displayed automatically unless `--no-plot` is used)

## Categories

- **medicine** - Benign medical guidance questions
- **law** - General legal information questions
- **coding** - Software development choices
- **gen_knowledge** - General decision-making scenarios
