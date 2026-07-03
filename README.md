# magicpin AI Challenge

This repo contains the Vera challenge bot.

## Files

- `bot.py` - the bot server
- `dataset/generate_dataset.py` - builds the expanded dataset
- `judge_simulator.py` - local test harness
- `challenge-brief.md` - what to build
- `challenge-testing-brief.md` - how it is tested
- `examples/` - API examples and case studies

## What the bot does

The bot builds the next WhatsApp message from four inputs:

- category
- merchant
- trigger
- optional customer

It must stay stateful, grounded in the received context, and return the next message with a clear CTA.

## Testing setup

The judge calls these endpoints:

- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`

The evaluation flow is:

1. Warmup with health and metadata checks, then base context load.
2. Test window with 5-minute ticks and context updates.
3. Adaptive injection of fresh digest items, metric shifts, triggers, and customer scopes.
4. Replay tests for auto-replies, intent transitions, and hostile replies.
5. Score report with message scores, logs, and transcripts.

Limits:

- 30 second timeout
- 10 requests per second
- 500 KB context cap
- up to 20 actions per tick

## Run

Start the bot:

```powershell
$env:PORT='8080'
py -3 bot.py
```

Generate the dataset:

```powershell
py -3 dataset\generate_dataset.py --seed-dir dataset --out dataset\expanded
```

Run the judge:

```powershell
$env:BOT_URL='http://127.0.0.1:8081'
py -3 judge_simulator.py
```

## Notes

- Use `py -3` on Windows.
- The bot supports `POST /v1/teardown` to clear state.
- The judge works with `dataset/expanded` when present.
