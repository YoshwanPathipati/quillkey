"""Vocabulary upgrader: flags weak/overused words with stronger options."""

import re

# word -> (alternatives, nuance note)
WEAK_WORDS: dict[str, tuple[list[str], str]] = {
    "good": (["compelling", "robust", "effective"], "pick by what makes it good — persuasive, sturdy, or results-driven"),
    "bad": (["flawed", "harmful", "ineffective"], "name the failure mode instead of the verdict"),
    "big": (["substantial", "significant", "vast"], "scale of amount, importance, or physical size"),
    "small": (["minor", "modest", "negligible"], "importance vs. size vs. near-zero"),
    "use": (["apply", "leverage", "deploy"], "apply a method, leverage an advantage, deploy a resource"),
    "get": (["obtain", "earn", "receive"], "effort implied: earn > obtain > receive"),
    "make": (["create", "produce", "build"], "creative, industrial, or constructive"),
    "very": (["(cut it)", "exceptionally", "remarkably"], "usually the sentence is stronger with it deleted"),
    "really": (["genuinely", "notably", "(cut it)"], "often filler — delete and reread"),
    "nice": (["thoughtful", "polished", "welcoming"], "say what made it nice"),
    "thing": (["factor", "element", "aspect"], "name the category the 'thing' belongs to"),
    "stuff": (["material", "details", "work"], "vague — replace with the actual noun"),
    "a lot": (["considerably", "frequently", "many"], "quantity of degree, time, or count"),
    "interesting": (["surprising", "counterintuitive", "revealing"], "say WHY it holds attention"),
    "important": (["critical", "decisive", "foundational"], "how important, and to what"),
    "show": (["demonstrate", "reveal", "illustrate"], "prove, uncover, or exemplify"),
    "help": (["enable", "accelerate", "streamline"], "what kind of help — unlocking, speeding, simplifying"),
    "look at": (["examine", "evaluate", "investigate"], "depth of scrutiny rises left to right"),
    "think": (["believe", "estimate", "contend"], "conviction, calculation, or argument"),
    "said": (["argued", "noted", "acknowledged"], "keep 'said' for neutral reporting; upgrade when stance matters"),
}

_PATTERNS = {
    word: re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE) for word in WEAK_WORDS
}


def find_weak_words(text: str) -> list[dict]:
    """Return vocabulary suggestions in the shared suggestion format."""
    suggestions = []
    for word, (alts, nuance) in WEAK_WORDS.items():
        for m in _PATTERNS[word].finditer(text):
            suggestions.append(
                {
                    "source": "vocabulary",
                    "error_type": "style",
                    "style_type": "weak_word",
                    "offset": m.start(),
                    "length": m.end() - m.start(),
                    "original": m.group(0),
                    "suggestion": alts[0],
                    "alternatives": alts,
                    "explanation": f'"{word}" is overused — consider: '
                    + ", ".join(alts) + f" ({nuance})",
                    "rule_id": "VOCAB_UPGRADE",
                }
            )
    return suggestions
