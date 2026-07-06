# Business Case -- A Private, Grounded Understanding Engine

**Version:** 0.1 (draft for discussion)
**Audience:** prospective design partners in intelligence/investigations, due
diligence / M&A, and legal / discovery.
**Date:** 2026-06-25
**Encoding:** ASCII only.

> This is a starting draft meant to be marked up. Bracketed `[your number here]`
> prompts are where a partner's own figures make the case concrete.

---

## 1. In one line

We turn a body of documents -- yours, in your environment -- into an
**interconnected, time-aware, fully sourced knowledge graph you can explore**,
where every fact traces back to the exact passage it came from and **nothing is
invented**. It runs **entirely on your own hardware**. No data leaves your walls.

It is not a chatbot and not a search box. It is an **understanding engine**.

---

## 2. The problem we solve

Analysts, investigators, and deal/legal teams all do the same expensive thing by
hand: read large, messy, scattered document sets and reconstruct **who did what,
when, with whom, and how it connects**. Three pains compound:

1. **Context collapse.** The answer is never in one document. It is spread across
   thousands, and a person has to hold the connections in their head, across time
   and entities, tab by tab. Most of the work is *re-assembly*, not reading.
2. **Trust.** General AI tools (the cloud chatbots) are fluent but **invent,
   blend, and cite unreliably**. In your work, an unsourced or fabricated claim is
   not a nuisance -- it is a liability, a blown filing, a bad call. You cannot put
   "the AI said so" in front of a client, a court, or a principal.
3. **Confidentiality.** Your most valuable corpora -- a target's data room, a
   privileged document set, source material -- **cannot be sent to a third-party
   cloud**. For much of this work, "it phones home" is an instant disqualifier.

The result today: smart, expensive people spend the majority of their time on
manual assembly, and the tools that could help are either untrustworthy, not
private, or both.

---

## 3. Why the current options fall short

- **Cloud LLM chat (ChatGPT/Copilot/etc.):** hallucinates, cites poorly, is
  generic to your domain, and sends your data off-premises. Fails on trust *and*
  confidentiality.
- **Manual review + analysts:** trustworthy but slow and unscalable; the
  connection-finding lives in one person's head and leaves when they do.
- **Legacy eDiscovery / review platforms:** good at search and tagging, weak at
  *synthesis* -- they find documents, they do not assemble the grounded, time-aware
  picture across them, and they are not built around explainable connection.
- **Enterprise knowledge-graph tooling:** powerful but heavy, manual to populate,
  expensive, and not designed for fast exploration by a working analyst.

The gap none of them fill: **automated, grounded, time-aware connection-finding
that is private by construction and shows its work.**

---

## 4. What we offer

Three things that only make sense together:

1. **The engine.** Point it at a corpus; it reads every document and builds a
   knowledge graph -- entities, relationships, and a precise temporal placement for
   each fact -- with a hard rule: **every item is grounded to a source passage and
   nothing is added from the model's memory.** A claim that cannot be pointed at a
   source is not stored or shown.
2. **A specialized ontology for your domain.** The engine is general; the value is
   a vocabulary tuned to your work -- the entity types, relationships, and the
   *meaning of dates* that matter to you (e.g. a contract's *executed* vs
   *terminated* date; an event's *observed* vs *occurred* date). This is what makes
   the output correct and useful in your field rather than generic.
3. **A private, grounded graph that is yours and compounds.** Everything is built
   and stored **on your hardware**. The graph grows as you work, is reusable across
   matters/cases/investigations, and never leaves your environment.

You explore it three ways, all off the same grounded understanding:
- **Reader** -- read a document with the *relevant connected material* surfaced
  alongside each passage, each with a one-line, sourced reason it is relevant.
- **Timeline** -- one unified, source-spanning timeline of everything that
  happened, with **contemporaneous events from related material** placed beside it,
  filterable by theme, and every item badged with where it came from.
- **Graph** -- the connection map itself: a central spine with related entities
  and documents radiating out, so you can see and follow the structure directly.

Across all three, **lenses/filters** let an analyst dial the view to a viewpoint
(financial, legal, operational, etc.) and control how much surfaces -- from "just
the essentials" to "show me everything."

---

## 5. How it works in your environment (plainly)

1. You point it at a document set, inside your network.
2. Small AI models running **on your own GPU** read and structure the material into
   the grounded graph -- in the background, not while you wait.
3. You open any document, entity, or date and explore: connected material, a
   timeline, and the graph -- every claim clickable back to its exact source.
4. The expensive understanding (who/what/when/connections) is computed **once and
   reused**; opening a new thread of the investigation re-uses it instantly.
5. Nothing is ever sent off your machines.

---

## 6. What changes for you (by vertical)

> The structure is the same; the ontology and the questions differ. Partner-specific
> metrics go in the brackets.

**Intelligence / investigations.** Connect actors, events, and locations across
siloed sources and see the picture over time -- including parallel activity that
never directly references itself. Move from manual link-charting to a graph that
assembles itself, with provenance for every link. *Today this takes
`[X]` analyst-days per investigation; the goal is to compress the assembly phase
materially while improving auditability.* Local/air-gapped operation is native.

**Due diligence / M&A.** Turn a chaotic data room into a structured, time-ordered
picture of the target -- entities, obligations, events, red flags -- under deal-clock
pressure, with every finding sourced for the committee. *Diligence routinely spans
`[thousands]` of documents in `[weeks]`; the goal is faster coverage with fewer
missed connections and a defensible trail.*

**Legal / contracts / discovery.** Build the grounded chronology and the
party/obligation map across a large document set, with citation to the exact
passage for every fact -- the thing review platforms make you assemble by hand.
*Document review is typically the largest line item in a matter; the goal is to cut
assembly time while raising confidence that nothing material was missed.*

In all three: the output is **defensible** (sourced), **private** (local), and
**reusable** (a graph you keep), which is exactly what generic cloud AI cannot
offer.

---

## 7. Why "local + grounded" is the unlock, not a constraint

These two choices are usually framed as limitations. For your work they are the
whole point:

- **Grounded by construction** means the system *structurally refuses to invent*.
  Trust is not a setting you hope holds; it is how the pipeline is built. That is
  what lets the output stand up to a client, a court, or a principal.
- **Local by construction** means confidentiality is not a policy promise but a
  physical fact -- the data never leaves. It also means no per-query cost, no rate
  limits, and it works offline / air-gapped.

A cloud chatbot cannot match this without ceasing to be a cloud chatbot. This is a
**category** difference, not a feature gap.

---

## 8. Why it gets better the more you use it

- Your **specialized ontology** sharpens with use -- the vocabulary of your domain,
  tuned once and improving, is hard to replicate and is *yours*.
- Your **grounded graph accumulates** across matters and investigations, so prior
  work compounds instead of evaporating when a person or a project ends.
- Both stay in your environment. The combination -- a tuned ontology plus an
  accumulating private graph -- is durable institutional knowledge you own.

---

## 9. Why now

Until very recently, AI capable of reading and structuring documents this way only
ran in the cloud, at cost, with your data leaving the building. Small AI models on
ordinary professional GPUs have **just crossed the line** where this extraction and
reasoning can run **locally, privately, and affordably**. The thing that was
impossible on-premises two years ago is now practical on a single workstation. That
timing is the opening.

---

## 10. What a design partnership looks like

We are selecting a small number of design partners to build the first
domain-tuned versions with.

**You provide:** access to a representative corpus *within your environment*, a
few hours of domain expertise to shape the specialized ontology, and candid
feedback on real work.

**You get:** an early, domain-tailored capability built around *your* problem;
direct influence over the roadmap; and preferential terms. The engine and your
data stay on your hardware throughout.

**We provide:** the engine, the ontology tuning, and hands-on setup, with a tight
feedback loop. We start narrow -- one workflow, one corpus, one set of questions --
and prove value before widening.

---

## 11. What this is not (so expectations are clear)

- **Not a chatbot.** It does not answer in fluent prose you have to trust. It shows
  grounded structure you can verify.
- **Not cloud.** Nothing is sent out; that is non-negotiable by design.
- **Not a replacement for judgment.** It does the assembly so your experts spend
  their time on the analysis only they can do. It augments and shows its work.
- **Not magic on day one.** A specialized ontology is built and tuned with you;
  early scope is deliberately narrow so value is real before it is broad.

---

## 12. The ask

If grounded, private, time-aware connection-finding over your own documents would
change how your team works, we would like to build the first version of it **with
you**. The first conversation is simply: *what corpus, what questions, what does
"good" look like* -- and we will scope a narrow first workflow from there.

---

## Appendix -- the proof-of-concept

The engine is first being proven on a complete encyclopedic corpus (Wikipedia) on a
single workstation: reading articles with connected context, a unified cross-article
timeline, and a navigable knowledge graph -- entirely local, entirely grounded. It
is the public, inspectable demonstration that the engine works; your version points
the same engine at your corpus with your ontology.
