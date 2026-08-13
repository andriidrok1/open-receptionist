# The owner interview — 20 questions that become the config

One sitting with the owner — over coffee or on a call — and you leave with
everything `businesses/<client>.yaml` needs. Field names in brackets refer to
`businesses/_template.yaml`, which documents each one.

Write down the owner's words verbatim where you can. The agent will speak
this business's language, and the owner's own phrasing ("we don't do gas
lines", "whoever's home pays") is better prompt material than your summary
of it.

## Identity

1. What's the business called — and is that how you SAY it on the phone?
   (People write "R&R Plumbing LLC" and answer "Rapid Response".) → `name`, `brand`
2. What city, and what timezone if it isn't obvious? → `city`, `tz`
3. What should the assistant be called? Any name the owner likes — callers
   will address it. → `agent_name`
4. Who is answering, in one phrase: a dispatcher? a receptionist? the front
   desk? → `persona`
5. How do you talk to callers here — short and calm? warm and chatty? What
   would sound WRONG coming from your front desk? → `tone`, `speech.bad/good`
6. What do you call a caller — guest, patient, customer, client? → `customer`

## What the business does (this becomes `knowledge`)

7. What do you do, in one paragraph? What do people most often call about?
8. What do you NOT do that people keep asking for? (This list prevents the
   worst failure: booking a job the business cannot perform.)
9. What must a caller be told even if they don't ask? (A visit fee, a
   cancellation policy, "someone must be home".) When — first minute, or at
   booking?
10. What are the prices you're willing to say on the phone, and which ones
    must never be quoted? ("The visit is $99, repairs are priced on site.")
11. How do people pay? Do you ever take card numbers by phone? (The agent
    should refuse to — confirm the owner agrees.)
12. Is there any call where safety comes first — gas smell, injury, flooding?
    What exactly should be said? (This goes at the TOP of `knowledge`,
    imperative form.)
13. Which calls should go straight to a human callback — billing, complaints,
    contracts? → `escalation_examples`
14. What words do callers use that a machine might mishear — street names,
    service jargon, brand names? → `keyterms`

## Booking

15. What does a booking mean here: an appointment at a time you set (slots —
    clinic, barbershop, service visit) or a stay the customer bounds (nights —
    hotel, rental)? → `booking.granularity`
16. What exactly can be booked? For each: what do callers call it (aliases!),
    how long does it take, what does it cost, and how many can run at the
    same time? (For a crew business, capacity = trucks on shift — and if one
    crew does every service, model ONE resource, or you will double-book the
    crew.) → `resources`
17. Your hours, day by day — and does booking stop before closing? → `hours`
18. How far ahead do you take bookings? → `horizon_days`
19. What must you know before the visit is worth making? Address? What's
    wrong? Access? Anything that means "don't book" — no adult home, outside
    your service area? → `intake`, `service_area`, `refuse_if`
20. Do you promise an exact time or a window ("between nine and ten")?
    → `arrival_window_min`

## Afterwards

- Fill the YAML the same evening while the answers are fresh; copy the
  closest existing config rather than starting blank.
- Read the `knowledge` block back to the owner — out loud. Every sentence
  they wince at, fix now; the agent will say it a hundred times.
- Run `python eval.py` scenarios against the new config, then make a browser
  call yourself (`python bot.py`, localhost:7860/client) and try the three
  hardest questions from the interview.
- Only then connect the number (docs/telephony.md).
