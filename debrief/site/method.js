"use strict";

// How the analysis was done, plus the few metrics it invented. {placeholders} are filled
// from the data's meta block, so the numbers match whatever run was exported.

const METHOD = {
  question: "Where do engineers have to step in and steer their coding agents, and what would stop that? Entire already records every session, so the answer is in the data. Here's how we got it out.",
  steps: [
    {
      id: "collect", title: "Collect the sessions", short: "Collect", glance: ["{sessions}", "sessions"],
      lede: "Pull agent sessions from Entire and join them to GitHub.",
      text: [
        "- Every session in {repo} from {from} to {to} with at least two steps in Claude Code, Codex or Pi: {sessions} of {window_sessions}. The rest are one-step sessions or other harnesses.\n- Each session's commits link it to its PR, so CI runs and review comments trace back to the session that wrote the code.",
      ],
      facts: [["{messages}", "messages"], ["{tool_calls}", "tool calls"], ["{prs}", "PRs"], ["{checks}", "CI runs"], ["{review_comments}", "review comments"]],
    },
    {
      id: "normalize", title: "Put three harnesses in one shape", short: "Normalize", glance: ["{human_turns}", "human messages"],
      lede: "Turn three log formats into one, and keep only what people typed.",
      text: [
        "- Claude Code, Codex and Pi each log differently. All three become one table of messages and one of tool calls.\n- Slash-command expansions, skill text and hook output arrive looking like user messages. They're set aside; counted as a person, they'd inflate every rate.",
      ],
    },
    {
      id: "label", title: "Label every message", short: "Label", glance: ["{labels}", "labels"],
      lede: "[Jev](https://typesafe.ai) answers the same questions about every human message.",
      text: [
        "It sees the message plus everything the agent did since the person's last one, and answers:",
        "1. **Intent:** What was the message doing?\n2. **Correction:** Did it correct the agent, and how?\n3. **Pointer:** What did it point the agent to?\n4. **Reversal:** Did it reject a design the agent had already built?\n5. **Bug:** Did it catch a bug in the agent's work?\n6. **Overlooked:** Did it tell the agent something it could have found itself?\n7. **Left unfiled:** Had the agent flagged a problem and left it?",
        "Also labeled:",
        "- **Each session:** task type, how it opened, how specific the opening was, whether the agent planned first, whether tests changed, outcome.\n- **Each failed CI run:** why it failed.\n- **Each review comment:** what it was about, and whether it held up.",
        "Labels below {min_conf} confidence are dropped.",
      ],
    },
    {
      id: "find", title: "Turn labels into findings", short: "Find", glance: ["{findings}", "findings"],
      lede: "Claude Opus reads the labels from seven angles and writes findings, each citing real examples.",
      text: [
        "The seven angles:",
        "- Code areas that draw corrections\n- Knowledge the agent was missing\n- Bugs found and left unfiled in the session\n- Correction patterns\n- Prompt habits\n- CI failures\n- Review comments",
        "The rules:",
        "- Each angle gets counts plus real examples, at most five from any one session.\n- Every finding must cite its examples by ID. Citations are checked, and a finding left with none is dropped.",
      ],
    },
    {
      id: "recommend", title: "Merge findings into ideas", short: "Ideas", glance: ["{recommendations}", "ideas"],
      lede: "Combine findings that describe the same problem into one idea for a change.",
      text: [
        "- One problem often shows up from several angles. A stale code comment can be a hotspot, a correction pattern and a knowledge gap at once.\n- Each idea gets a type (agent instructions, tests, CI, review bots, team practice, or a bug to file).\n- Reach, meaning sessions and people touched, is counted from the evidence, not taken from the model. Every list sorts by it.\n- A second, cheaper model pass checks every cited item against its idea. Items that don't support it are dropped, and reach counts only what's left.\n- The two Opus passes and the check together cost about $2.40 at API prices.",
      ],
    },
  ],
};

// Metrics this analysis defines. `page: true` entries appear on the How it works page;
// the rest only supply ⓘ tooltips and link to the step that explains them (`anchor`).
const DEFS = [
  {
    id: "correction_rate", term: "Agent corrected (%)", page: true, better: "lower",
    short: "Share of human messages that corrected the agent's work: said it was wrong, had it redone, or caught a bug. Labels count only at confidence {min_conf} or higher.",
    def: "Share of human messages that corrected the agent's work. Labels count only at confidence {min_conf} or higher. A message counts once, even if it does more than one of these:",
    list: [
      ["Said it was wrong", "Told the agent something it did or said was wrong."],
      ["Had it redone", "Rejected an approach the agent had already built. Fixing one detail doesn't count."],
      ["Caught a bug", "Pointed out a bug or unwanted output in the agent's work."],
    ],
  },
  {
    id: "overlooked_info", term: "Agent overlooked (%)", page: true, better: "lower",
    short: "Share of human messages that pointed the agent to information it already had access to but didn't find.",
    def: "Share of human messages that told the agent something it could have found itself: code, test results, git, PR, CI or trail state, docs. The work isn't necessarily wrong; the agent just didn't look.",
    example: "“there are more findings”, sent right after the agent reported none open.",
  },
  {
    id: "found_not_filed", term: "Found, not filed", page: true,
    short: "The agent named a problem outside its task and left it. It wasn't filed during the session.",
    def: "The agent spotted a problem outside its task, such as a test failing on main, and left it. It wasn't filed during the session. Each one becomes a possible-bug idea.",
  },
  {
    id: "shipped_rate", term: "Shipped rate", page: true, better: "higher",
    short: "Share of sessions that ended with work committed or a PR ready.",
    def: "Share of sessions that ended with work committed or a PR ready. Judged from how the transcript ends, not whether the PR later merged.",
  },
  {
    id: "mistake_types", term: "What the agent got wrong", page: true,
    short: "For messages that said the agent was wrong, the kind of mistake.",
    def: "For messages that said the agent was wrong, the kind of mistake:",
    list: [
      ["Wrong assumption", "Assumed something false."],
      ["Missed requirement", "Left out something that was asked for."],
      ["Scope", "Did too much or too little."],
      ["Factual error", "Stated something untrue."],
      ["Skipped a step", "Didn't run tests, or didn't ask first."],
      ["Convention", "Broke a style or project convention."],
      ["Unclassified", "The model wasn't at least 50% sure."],
    ],
  },
  {
    id: "hotspot", term: "Where corrections happen", page: true,
    short: "Directories the agent wrote to just before a person corrected it.",
    def: "Directories the agent wrote to just before a person corrected it. One session in a directory usually means one hard task, not a weak spot.",
  },
  // Tooltip-only entries.
  { id: "by_model", anchor: "collect", short: "Grouped by model, across harnesses. 1M-context variants count with their base model. Models with fewer than 10 sessions are left out." },
  { id: "sessions", anchor: "collect", short: "Agent sessions with at least two steps in Claude Code, Codex or Pi: {sessions} of {window_sessions} between {from} and {to}." },
  { id: "settled_pr", anchor: "collect", short: "Only PRs that merged or closed count, so open PRs mid-iteration don't skew the numbers." },
  { id: "ci_causes", anchor: "label", short: "Why a check failed, judged from the tail of its log: infra, test regression, flaky test, build error or timeout. Passed on re-run is different: it's seen, not judged, when the same check later passed on the same commit." },
  { id: "ci_where", anchor: "label", short: "Where each failed run broke, read from its log: the failing Go package, test and file. The log names the test that failed, not the change that caused it." },
  { id: "review_where", anchor: "collect", short: "Inline review comments by the file type and directory they point at. The table shows directories with at least 5 comments, sorted by how often reviewers were wrong." },
  { id: "held_up", anchor: "label", short: "Whether a review comment turned out right, judged from its thread: valid and fixed, valid and open, outdated, wrong, or unclear." },
  { id: "turn_kinds", anchor: "label", short: "What each human message was doing: a question, a new request, an approval, a correction, an answer, or a status check. Counts every label at any confidence, so Correction here runs higher than Agent corrected, which counts labels at {min_conf} or higher." },
  { id: "prompt_styles", anchor: "label", short: "How the person opened the session. Compared only on sessions with a merged or closed PR, so the counts are smaller than elsewhere on this page; small groups." },
  { id: "skills_use", anchor: "normalize", short: "Typed: a person ran a /command. Auto-loaded: the agent loaded a skill itself." },
  { id: "reach", anchor: "recommend", short: "How many sessions and people an idea's evidence comes from. Lists are sorted by it." },
  { id: "per_person", anchor: "collect", short: "Usage only. Correction rates aren't broken out per person." },
];

// The "Next steps" page: what it would take to turn this prototype into a product.
const NEXT_STEPS = {
  title: "Caveat emptor",
  intro: "I did some basic training to get reasonable results out of this app but ultimately it's just a prototype so I'm sure it's full of false positives.",
  lead: "In order to turn this into a real, shippable product I would:",
  items: [
    {
      title: "Make every result trustworthy",
      text: "The labeling and aggregation steps in this product are functional, but rough and only semi-informed. A real product would need this work done properly, starting with a hand-picked set of evals for each label, type of finding, and recommendation set. We would need to score them on precision and recall and retrain the prompts until both are high. At a higher level of investment, post-training a small model could make sense.",
    },
    {
      title: "Make a feedback loop",
      text: "Add a Done/Not useful/Wrong button on every idea. Feed the info you get from those buttons back into the system, possibly making the learnings repo-specific.",
    },
    {
      title: "Validate the need & ideal delivery approach",
      text: "Internal dogfooding would be a good start but I'd quickly want to roll this out to a few test clients and see 1) if it's useful, and 2) who ends up using it.\n\nDepending on who uses it and how much, we could adapt the packaging & delivery:\n\n- An always-on dashboard\n- A live feed of ideas into a Slack channel\n- A weekly email to a CTO/VP Eng\n- etc.",
    },
    {
      title: "Tie it to the rest of the Entire products",
      text: "Could be sold on its own, included for free, or offered at a discount for whatever the company wants to grow the most. Figuring out #3 (and the team's other priorities) will help inform how this should be packaged.",
    },
    {
      title: "Compile benchmarks, publish them",
      text: "Across tens of thousands of sessions, Entire could publish anonymized rankings and best practices:\n\n- Which harnesses and models need the fewest corrections\n- Skill impact on error rates\n- Recs on CI setup, testing, etc.\n\nAll of this creates content we could use at the top of the funnel to bring in more users.",
    },
    {
      title: "Let people compare their team with others",
      text: "Give a CTO a private view of how their team's workflow compares with similar teams across correction rates, overlooked info, token efficiency, etc. Give them hard evidence they can take to their leadership team or board to show the team is doing well or to argue for spending on tooling, reducing tech debt, etc.",
    },
  ],
};
