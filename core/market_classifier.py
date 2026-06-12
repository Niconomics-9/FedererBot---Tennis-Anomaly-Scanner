"""
Market classification helpers for alert quality.

Providers still store every raw tennis market. This module decides whether a
snapshot is the kind of market worth alerting on: currently match-winner
markets only, with props/futures/completed markets classified and rejected.
"""

from dataclasses import dataclass
import re

from market_providers.models import MarketSnapshot


class MarketType:
    MATCH_WINNER = "match_winner"
    SET_WINNER = "set_winner"
    TOTAL = "total"
    HANDICAP = "handicap"
    TOURNAMENT_FUTURE = "tournament_future"
    COMPLETED = "completed"
    MALFORMED = "malformed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarketClassification:
    market_type: str
    player_a: str | None
    player_b: str | None
    selection: str | None
    match_key: str
    is_actionable: bool
    reject_reason: str | None = None


_VS_RE = re.compile(r"\bvs\.?\b", re.IGNORECASE)
_SPACES_RE = re.compile(r"\s+")
_PARENS_RE = re.compile(r"\([^)]*\)")

_TOTAL_KEYWORDS = (
    " o/u ",
    " over/under ",
    " total ",
    " totals ",
    "games o/u",
    "sets o/u",
    "match o/u",
)
_HANDICAP_KEYWORDS = ("handicap", "spread")
_FUTURE_KEYWORDS = (
    " to win ",
    " winner",
    " win wimbledon",
    " win australian open",
    " win french open",
    " win roland garros",
    " win us open",
)
_BAD_SELECTION_WORDS = {
    "completed",
    "handicap",
    "match",
    "set",
    "sets",
    "total",
    "winner",
}


def classify_snapshot(snapshot: MarketSnapshot) -> MarketClassification:
    return classify_market(snapshot.match_name, snapshot.player_name)


def classify_market(title: str, player_name: str = "") -> MarketClassification:
    title_clean = _clean_text(title)
    lower = f" {title_clean.lower()} "
    player_a, player_b = _extract_participants(title_clean)
    market_type = _market_type(title_clean, lower, player_a, player_b)
    selection = _selection_name(title_clean, player_name, player_a, player_b)
    match_key = _match_key(player_a, player_b)

    reject_reason = _reject_reason(market_type, player_a, player_b, selection)
    return MarketClassification(
        market_type=market_type,
        player_a=player_a,
        player_b=player_b,
        selection=selection,
        match_key=match_key,
        is_actionable=reject_reason is None,
        reject_reason=reject_reason,
    )


def extract_selection_name(title: str, player_name: str = "") -> str:
    """
    Best-effort player selection used by providers when a market has an
    "A vs B" title. For binary match-winner markets, YES usually maps to the
    first listed player; prop markets are still filtered before alerting.
    """
    classification = classify_market(title, player_name)
    if classification.selection:
        return classification.selection
    return _legacy_selection(title)


def _market_type(
    title: str,
    lower: str,
    player_a: str | None,
    player_b: str | None,
) -> str:
    stripped = title.strip().lower()
    if "completed match" in lower or " completed " in lower:
        return MarketType.COMPLETED
    if any(keyword in lower for keyword in _HANDICAP_KEYWORDS):
        return MarketType.HANDICAP
    if any(keyword in lower for keyword in _TOTAL_KEYWORDS):
        return MarketType.TOTAL
    if stripped.startswith("set ") and " winner" in lower:
        return MarketType.SET_WINNER
    if " set " in lower and " winner" in lower:
        return MarketType.SET_WINNER
    if player_a and player_b:
        return MarketType.MATCH_WINNER
    if stripped.startswith("will ") or any(keyword in lower for keyword in _FUTURE_KEYWORDS):
        return MarketType.TOURNAMENT_FUTURE
    if not title.strip():
        return MarketType.MALFORMED
    return MarketType.UNKNOWN


def _extract_participants(title: str) -> tuple[str | None, str | None]:
    will_beat = re.match(
        r"^\s*will\s+(.+?)\s+(?:beat|defeat)\s+(.+?)\??\s*$",
        title,
        flags=re.IGNORECASE,
    )
    if will_beat:
        left = _clean_player(will_beat.group(1))
        right = _clean_player(will_beat.group(2))
        if _looks_like_player(left) and _looks_like_player(right):
            return left, right

    candidates = _participant_candidates(title)
    for candidate in candidates:
        parts = _VS_RE.split(candidate, maxsplit=1)
        if len(parts) != 2:
            continue
        left = _clean_player(parts[0])
        right = _clean_player(parts[1])
        if _looks_like_player(left) and _looks_like_player(right):
            return left, right
    return None, None


def _participant_candidates(title: str) -> list[str]:
    parts = [part.strip() for part in title.split(":") if part.strip()]
    candidates: list[str] = []
    candidates.extend(reversed(parts))
    candidates.append(title)
    return candidates


def _selection_name(
    title: str,
    player_name: str,
    player_a: str | None,
    player_b: str | None,
) -> str | None:
    cleaned_player = _clean_player(player_name)
    if player_a and _same_name(cleaned_player, player_a):
        return player_a
    if player_b and _same_name(cleaned_player, player_b):
        return player_b
    if cleaned_player and _looks_like_player(cleaned_player):
        tokens = set(_name_tokens(cleaned_player))
        if not tokens.intersection(_BAD_SELECTION_WORDS):
            return cleaned_player
    if player_a:
        return player_a
    return None


def _reject_reason(
    market_type: str,
    player_a: str | None,
    player_b: str | None,
    selection: str | None,
) -> str | None:
    if market_type != MarketType.MATCH_WINNER:
        return f"market_type={market_type}"
    if not player_a or not player_b:
        return "missing_players"
    if not selection:
        return "missing_selection"
    return None


def _match_key(player_a: str | None, player_b: str | None) -> str:
    if not player_a or not player_b:
        return ""
    names = sorted((_canonical_name(player_a), _canonical_name(player_b)))
    return "|".join(names)


def _same_name(left: str, right: str) -> bool:
    if not left or not right:
        return False
    left_key = _canonical_name(left)
    right_key = _canonical_name(right)
    return left_key == right_key or left_key in right_key or right_key in left_key


def _looks_like_player(value: str) -> bool:
    tokens = _name_tokens(value)
    if not tokens:
        return False
    if len(value) > 80:
        return False
    if set(tokens).issubset(_BAD_SELECTION_WORDS):
        return False
    return any(len(token) >= 2 for token in tokens)


def _clean_text(value: str) -> str:
    return _SPACES_RE.sub(" ", value or "").strip()


def _clean_player(value: str) -> str:
    value = _PARENS_RE.sub("", value or "")
    value = re.sub(r"\b(?:to win|wins?|winner|market)\b", "", value, flags=re.IGNORECASE)
    value = value.replace("?", " ")
    value = value.strip(" -:|/")
    return _clean_text(value)


def _canonical_name(value: str) -> str:
    return " ".join(_name_tokens(value))


def _name_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z]+", value.lower())


def _legacy_selection(title: str) -> str:
    q = title.strip()
    lower = q.lower()
    if lower.startswith("will "):
        rest = q[5:]
        for sep in (" win ", " to win ", " beat ", " defeat "):
            idx = rest.lower().find(sep)
            if idx != -1:
                return rest[:idx].strip()
        words = rest.split()
        return " ".join(words[:2]) if len(words) >= 2 else rest
    for sep in (" to win ", " win ", " - "):
        idx = lower.find(sep)
        if idx != -1:
            return q[:idx].strip()
    return q[:40]
