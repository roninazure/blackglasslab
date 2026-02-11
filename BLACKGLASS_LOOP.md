# BLACK GLASS LAB — Minimal Loop (v0)

1. Select a binary "market" (yes/no question) with a known resolution time.
2. Operator commits: prediction + confidence (0.50–0.99) + rationale.
3. Skeptic commits: counter-prediction + confidence + critique.
4. Resolve outcome (simulated in v0).
5. Auditor scores:
   - Accuracy (+1 correct, -1 wrong)
   - Overconfidence penalty if confidence > rolling accuracy
6. Persist:
   - market, predictions, confidences, outcome, scores, timestamps
7. Repeat.

Rule: everything must write to disk. No ephemeral runs.
