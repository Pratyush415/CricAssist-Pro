# Evaluation results

Run on 19 October 2026 with `cricassist eval` (questions in
`src/cricassist/evaluation/questions.json`). Gemini free tier: 14 answers from
`gemini-3.5-flash-lite`, 1 from `gemini-3.6-flash`, because each model's daily
free quota covers only part of a run. The demo uses `gemini-3.8-flash`.

**14 of 15 correct, 15 of 15 fully sourced** (target: at least 13 correct and
no unsourced numbers).

| Question | Type | Correct | Sourced |
|---|---|---|---|
| How many sixes did Virat Kohli hit at the 2022 T20 World Cup? | stats | yes | yes |
| How has Shami been over the years in terms of consistency? | stats | **no** | yes |
| How does Kohli fare against left-arm spin in T20s since 2022? | stats | yes | yes |
| Who were the best death-over bowlers in IPL 2025, and how does Bumrah compare? | stats | yes | yes |
| What was Virat Kohli's Test batting record? | stats | yes | yes |
| What is the highest individual score in IPL history? | stats | yes | yes |
| What is India's head-to-head record against Australia in Tests? | stats | yes | yes |
| What's a par first-innings score at the Wankhede in the IPL? | stats | yes | yes |
| How many times has Josh Hazlewood dismissed Virat Kohli? | stats | yes | yes |
| What is the lowest Test team total since 2001? | stats | yes | yes |
| How long was Dale Steyn ranked number one in Test cricket? | cannot answer | yes | yes |
| What is Rashid Khan's T20I bowling record for Afghanistan? | cannot answer | yes | yes |
| What did Virat Kohli say after his innings against Pakistan at the 2022 T20 World Cup? | context | yes | yes |
| Describe Jasprit Bumrah's bowling action and run-up. | context | yes | yes |
| Tell me about the 2023 World Cup final. | combined | yes | yes |

**The miss.** The Shami answer compared his formats with figures from the
tools but described consistency in words instead of quoting the measure the
tool gives (spread and median of yearly averages), and attributed swings to
"workload and conditions", which the data does not show.

## How grading works

- **Correct**: the answer contains the expected facts, checked against the
  database. For questions the data cannot answer, it must say so and must not
  invent anything (no ranking duration, no made-up quote).
- **Sourced**: every number in the answer appears in the question, a tool call
  or a tool result. Any other number can only have come from the model's
  memory or its own arithmetic.

## What the evaluation caught and fixed

Earlier runs failed the sourcing check in ways that led to fixes:

- The model stated a match count ("312 matches") that no tool returned. Record
  lists and leaderboards now report their coverage.
- It computed strike rates and per-format ratios itself. Those tools now
  return every common ratio.
- It looked up two bowling styles separately instead of the combined
  "left-arm spin" group, then rounded ("averaging over 60"). The tool
  description and the prompt now cover both.
- In a demo test it wrote a dismissal from memory ("c Maxwell b Cummins");
  the scorecard tool now gives every dismissal from the data ("c Head b
  Maxwell").

## Written context

The first runs (19 October) had an empty context database, because
ESPNcricinfo refuses automated collection, so the context questions passed by
saying no written context was available. On 22 October, with the interviewer's
approval, the context database was filled from Wikipedia (125 articles, 2,084
passages), the three questions were updated to expect answers that cite
Wikipedia with a link, and they were rerun on `gemini-3.7-flash`: all three
correct and fully sourced. The Bumrah question is now answered from his
Wikipedia article (short run-up, hyperextended elbow, front-on action), and the
2023 final combines the scorecard from the database with Wikipedia's account
of the match.

Numbers that appear only in a Wikipedia passage now count as unsourced: every
figure must come from the stats tools.
