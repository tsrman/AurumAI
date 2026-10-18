---
name: multistep-math
description: Computes a two-step result using two sequential shell commands, where the second command's input depends on the first command's output. To use, ask it to compute the two-step result.
---

# Multi-step skill

You must run exactly two commands, one after the other, and read the output
of each before deciding the next command:

1. Run: `python3 -c "print(6 * 7)"` — this prints a number N.
2. Run a second, different command: `python3 -c "print(N + 100)"`, replacing
   `N` with the exact number printed by step 1 (do not guess it, use the
   real printed value).

Do not skip a step. Report the final printed number as your answer, in
exactly this form: `FINAL RESULT: <number>`.
