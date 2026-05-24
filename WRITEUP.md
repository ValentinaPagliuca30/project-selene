# Project Selene — Write-up

## Architecture and design decisions

**WHAT IS MY GOAL?**

To produce a comprehensive map of how the colony's pods depend on each other and to assess the colony's operational resilience. The idea is to build an agent that discovers the colony network, crawls every pod endpoint, maps the infrastructure, analyzes it for systemic risks, and produces a report.

**WHAT DID I BUILD?**

*Diagram: `mapping.py` → `map.json` → `reporting.py`*

My mapping agent discovers the colony and writes its findings to `/rover/output/map.json`.

My reporting agent reads `map.json` and produces an analysis report.

I started by writing `mapping.py`. It is entirely deterministic. It discovers pods through a port scan on the documented range. I initially considered using `nmap`, but that would be an anti-pattern in any real network environment: IDS tools would flag it, segmentation could block it, and in a security context, that matters. I also ruled out hardcoded mapping because it removes any meaningful auto-discovery. In production, I would use DNS SRV-record service discovery.

Once it finds the pods, the mapper traverses all declared dependencies and supply relationships breadth-first until every pod has been fully visited.

The output is `map.json`, which has four sections: pod data, relationships, discrepancies, and run metadata.

Pod data includes everything the API returned for each of the twelve pods — specs, logs, and messages — with the raw responses preserved alongside the normalized data so that nothing gets reinterpreted.

Relationships are recorded by claim. If both sides agree — for example, one pod says "I depend on this," and the other says "I supply this" — the edge has two entries and is confirmed. If only one side made the claim, the edge has one entry and is flagged as `disputed: true`.

Discrepancies are all the disputed edges collected into one list, with a plain-language description of what does not match.

Then I wrote `reporting.py`. It reads `map.json` and does two things in parallel.

First, it runs a set of deterministic graph analyses, such as identifying which pods are most depended upon, whether there are circular dependencies, which resources have only one supplier, and what happens if a pod goes down.

Second, it makes four targeted calls to an LLM: one to write the historical narrative from the logs, one to triage the discrepancies, one to draft recommendations, and one to produce the executive summary. Each call receives only the data relevant to that specific question. I also set all LLM calls to run at `temperature=0`, so that re-running the agent on the same `map.json` always produces the same report.

**WHY DID I BUILD IT THIS WAY?**

Latent Defense's framing was a useful starting point. The main idea is that if you give an LLM a raw infrastructure map and ask it to reason about failure modes and attack paths, it will try to process the space as language and may produce confident-sounding but unreliable answers.

So the principle I followed in the design was to assign structural reasoning — including graph traversal, cycle detection, and failure simulation — to deterministic algorithms, while leaving narrative, interpretation, and judgment to the model.

Three decisions follow directly from this principle.

- First, discovery uses a port scan for the reasons explained above.
- Second, for dispute handling, I had to find a compromise. Never adjudicating anything would overwhelm the human reviewer with noise. Auto-resolving everything with the LLM would risk silently getting a safety-relevant relationship wrong. So I chose a middle ground: the LLM can mark a dispute as `likely_benign` only when three things are all true — the resource is non-critical, confidence is high, and the explanation is intrinsically safe. Anything physical — power, water, atmosphere, medical supplies, or food — is marked as `needs_human_review` regardless of how confident the model is.
- Third, each LLM call receives only a scoped slice of the findings, never the full `map.json`. Passing everything into one prompt would defeat the purpose of the system. The whole reason to build a structured world model first is so that it can be queried precisely, rather than asking the model to hold the entire space in context.

## Issues I encountered and how I fixed them

After building my agent, I ran four rounds of review to verify whether it had produced correct outputs, identify any errors, and determine what fixes were needed.

The main errors I found were the following.

First, the narrative was mixing up two different metrics: unique consumers of a pod versus resources for which that pod is the sole supplier. As a result, it cited the wrong count in the report. To fix this, I added explicit instructions to the prompt specifying which metric is which and requiring the model to name the metric whenever it states a number.

Second, the narrative described Vault's relationships backwards, saying that Vault supplied something when it was actually the consumer. This happened because the `from = consumer` convention in the edge schema had never been stated explicitly. To fix this, I added a worked example to the prompt showing the convention with a concrete case.

Third, the narrative said that the colony made "three consolidation decisions," but there were actually four in the logs. To fix this, I extracted all consolidation events deterministically into a labeled list before the LLM call and instructed the model to use that list as its only source, rather than re-reading the logs itself.

A fifth event — Zephyr retiring its humidity reclamation loop — was not included in the extracted list because "retired" was not part of my keyword filter. I fixed this by extending the keyword set.

## What the agent found

The mapper found 39 relationships between pods. Of these, 16 — or 41% — were disputed: one side claimed the relationship, while the other did not acknowledge it.

The agent found two systems for which there is no fallback if they fail.

First, Aquifer is the colony's sole water hub. It runs at 91.6% of rated capacity, supplies six different resource flows to the rest of the colony, and has `backup_systems: 0`.

Second, Helios is the sole power source for eight pods, none of which have an alternative. The two systems are mutually dependent: Helios powers Aquifer, and Aquifer cools Helios.

Vault, the pod explicitly designated for emergency reserves, has decommissioned its water backup and coolant distribution and self-declared this in its own metadata.

The July 2094 safety review declared all pods nominal, but "nominal" only means that the pods are running, not that they are safe. Every backup had already been removed. The monitoring system had no way to see that.

Those backups were not removed all at once. Between October 2092 and February 2094, the colony made twelve separate consolidation-related decisions, each of which was a reasonable cost-saving measure or budget reallocation at the time. No single decision looks wrong in isolation. But each one removed a small piece of redundancy, and together they left the two most critical systems with nothing underneath them.

The analysis also found that Prometheus has a metadata field claiming that it draws water directly from Aquifer, but a 2093 log records that this connection was sealed and the flow was rerouted through Hydroponics' irrigation circuit. This creates a hidden shared dependency that is invisible from either pod's status.

The agent also found that Sentinel is the one pod that could survive a colony-wide failure: it has zero confirmed dependencies, 180 kW of independent solar power, and 120 L/day of ice harvest. It was commissioned 77 days after the founding cohort, which suggests that its independence was a deliberate late-stage design choice.

## What I would do with more time

This exercise was scoped as a three-to-five-hour build, so I prioritized the pieces that would most reduce LLM failure modes within that window.

The first thing I would implement with more time is multi-step cascade simulation. Right now, the analysis is mostly single-hop. This means it correctly identifies that Aquifer is critical and that it is connected to Helios, but it does not fully simulate the chain that would unfold afterward: Aquifer fails, Helios loses cooling, Helios goes down, and everything Helios powers goes dark.

I would also add a self-check loop after each LLM call that automatically validates numbers, edge directions, and event counts against the structured findings. The four errors I caught manually should be caught by the system itself.

Another important improvement would be prompt-injection hardening for log and communications ingestion. Logs and messages are untrusted inputs, and although I identified this as a possible attack vector, I did not have time to mitigate it within the scope of the project.

I would also add /status stub detection. Every pod always responds with "everything is fine, no alerts" — not because it actually is, but because that response is hardcoded and never changes. A robust agent should notice that all twelve pods return exactly the same answer and conclude that the endpoint is not saying anything useful, rather than trusting that "all good" as real information.

I also noticed, for example, that Zephyr has only four hours of backup power, which is a low number. My current check only flags systems with zero backup, so this issue was not included in the report. For this reason, I would add quantitative trend detection for weak signals such as Zephyr's four hours of backup power, Helios silicon feedstock being at 140% of the quarterly forecast, and panel degradation accumulating at 1.2% per year.

The final and biggest step would be turning the snapshot into something live: rebuilding the agent and the system as a continuous monitoring tool that can detect when a backup path silently disappears, with a query layer on top of the structured map.
