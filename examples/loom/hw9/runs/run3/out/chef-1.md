# Dinner plan: days 1-3

> **Please confirm before I go further.** LOOM marked this as a first request
> from plateplan (`confirmation: first_request`), so you need to OK this plan
> before it's treated as final or used for anything else (like a shopping order).
> Reply "OK" or tell me what to change.

## What I'm tuning for

Over the last 14 days your glucose has been steady overall (average 127 mg/dL,
variability 14.8%, 100% of the time in range), but meals push you up to a peak
of about **177 mg/dL**. That's still in range, but close to the top of it. So
the job for dinner is to keep the after-meal spike lower, not to overhaul
anything. Your constraints are **limit added sugar** and **prefer low-glycemic
foods**.

How each dinner does that:

- **Half the plate is non-starchy veg, a quarter protein, a quarter (or less)
  slow carbs** (legumes, barley, quinoa, whole-grain or al dente pasta), and
  never white rice, white bread or potatoes as the main carb.
- **Eat it in order:** veg/salad first, then protein, carbs last. That reliably
  softens the spike.
- **Fat, fibre and acid** (olive oil, nuts, vinegar or lemon) in every meal to
  slow how fast the carbs are absorbed.
- **No added sugar** in sauces or marinades, and no dessert built on sugar.
  If you want something sweet, have berries with plain Greek yogurt.
- **A 10-15 minute walk after dinner** if you can. Moving your muscles after
  eating is one of the easiest ways to cut the peak.

## Day 1: Lemon-herb salmon, lentils and greens

- Baked salmon (about 150 g) with lemon, dill and olive oil
- Warm green or Puy lentils (about ¾ cup cooked) with shallot and a splash of red-wine vinegar
- Roasted broccoli and a big handful of rocket
- Start with the greens, finish with the lentils.

*Why:* lentils are among the lowest-GI carbs there are, and the omega-3 fat in
the salmon plus the vinegar both slow digestion.

## Day 2: Chicken and vegetable stir-fry over barley

- Chicken thigh (about 150 g) or firm tofu, stir-fried with peppers, snap peas, mushrooms, bok choy, garlic and ginger
- Sauce: soy or tamari, rice vinegar, sesame oil and chili. **No honey, sugar, hoisin or sweet chili sauce** (they're where stir-fries hide added sugar)
- Pearl barley (about ½ cup cooked) in place of white rice, or cauliflower rice mixed half-and-half with it
- Top with toasted cashews or sesame seeds

*Why:* barley's soluble fibre (beta-glucan) gives a much flatter curve than
white rice, and a sauce with no sugar takes away the second source of the spike.

## Day 3: Turkey-bean chili with a side salad

- Lean ground turkey (or more beans for a vegetarian version) with kidney beans and black beans, tomatoes, onion, peppers, courgette, cumin and smoked paprika
- Top with avocado, a spoon of plain Greek yogurt, and lime
- Side salad with olive oil and vinegar. Eat it first
- Skip the cornbread and tortilla chips. If you want a crunch, use a few pumpkin seeds

*Why:* beans give a lot of protein and fibre for their carbs, and avocado adds
fat. It also makes leftovers, which helps on a busy night.

## Quick swaps
| Instead of | Use |
|---|---|
| White rice / potatoes | Barley, quinoa, lentils, beans, cauliflower rice |
| Sweet sauces (teriyaki, BBQ, hoisin) | Tamari + vinegar + garlic/ginger; spice rubs |
| Juice / soda with dinner | Water, sparkling water with lime, unsweetened tea |
| Sugary dessert | Berries + plain Greek yogurt, a few nuts |

*This is a meal plan, not medical advice. If your clinician has given you
specific carb or glucose targets, those come first.*

## What I got from LOOM and how I used it

- **Request:** task `meal_plan`, category `agent_for_user`, tier L2, fields
  `glucose_patterns` + `constraints`. That's exactly what this task allows, so I
  didn't ask for raw readings (L1), the raw signal (L0) or alerts. The request
  was **granted** (grant `g-2aa5ed2dc6`, valid until day 7, can be fetched
  again). plateplan's credit went from 60 to 61. Friction band is medium, which
  means **you must confirm on this first request**. That's why the plan opens by
  asking for your OK.
- **Data received:** a 14-day window with mean 127 mg/dL, CV 14.8%, after-meal
  peak 177 mg/dL and 100% time in range. Constraints were `limit_added_sugar`
  and `prefer_low_glycemic`.
- **How I used it:** the after-meal peak (177) was the main thing I tuned for.
  It led to low-GI carbs in small portions, the veg-first eating order,
  fat/fibre/acid in every meal, and the walk after dinner. The steady mean and
  CV are why the changes are moderate, not restrictive. The two constraints
  ruled out sweetened sauces and sugar-based desserts, and set which carbs I
  picked.
- **What I didn't get or do:** I didn't have per-meal readings, so I can't
  say which of your own past dinners caused the 177 peak. The plan is tuned to
  the overall pattern, not to particular foods. I made no further fetches and
  kept no data outside this plan.
