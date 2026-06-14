"""Турниры на выбывание (single elimination) с турнирной сеткой.

Сетка строится сразу целиком: первый раунд заполняется игроками (с автопроходом
для «байов», когда игроков не степень двойки), остальные раунды — пустые слоты,
которые заполняются победителями по мере подтверждения счёта.

Документ турнира в Mongo:
{
  id, name, game, prize, status: "active"|"finished",
  players: [str],
  champion: str|None,
  rounds: [ { name, matches: [
      { id, p1, p2, score1, score2, winner, confirmed }
  ] } ],
  created_at
}
id матча: "r{round}m{index}". Победитель матча r{ri}m{mi} попадает в матч
r{ri+1}m{mi//2}, слот p1 (mi чётный) или p2 (mi нечётный).
"""
from typing import Any, Optional

from bson import ObjectId

from config import now_kz
from db import tournaments_col


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


def _seed_order(size: int) -> list[int]:
    """Порядок посевов по позициям сетки (size — степень двойки).

    Классическая расстановка: сильные посевы разведены по сетке так, что
    «байы» (нижние посевы без игрока) всегда попадают против реального игрока,
    а не друг против друга. Возвращает список номеров посевов (1-based).
    """
    seeds = [1]
    while len(seeds) < size:
        m = len(seeds) * 2 + 1
        seeds = [x for s in seeds for x in (s, m - s)]
    return seeds


def _round_name(remaining: int) -> str:
    """remaining — сколько матчей в раунде."""
    return {1: "Финал", 2: "Полуфинал", 4: "Четвертьфинал"}.get(
        remaining, f"1/{remaining}"
    )


def _empty_match(rid: str) -> dict:
    return {"id": rid, "p1": None, "p2": None, "score1": 0, "score2": 0,
            "winner": None, "confirmed": False}


def _build_bracket(players: list[str]) -> list[dict]:
    """Строит все раунды сетки. Байы в первом раунде проходят автоматически."""
    size = _next_pow2(max(2, len(players)))
    # Раскладываем игроков по позициям сетки в порядке посева; недостающие
    # позиции (нижние посевы) остаются байами и проходят автоматически.
    slots: list[Optional[str]] = [
        players[seed - 1] if seed <= len(players) else None
        for seed in _seed_order(size)
    ]

    rounds: list[dict] = []
    first_matches = []
    for i in range(size // 2):
        p1, p2 = slots[i * 2], slots[i * 2 + 1]
        m = _empty_match(f"r0m{i}")
        m["p1"], m["p2"] = p1, p2
        # Автопроход, если у соперника нет (бай).
        if p1 and not p2:
            m["winner"], m["confirmed"] = p1, True
        elif p2 and not p1:
            m["winner"], m["confirmed"] = p2, True
        first_matches.append(m)
    rounds.append({"name": _round_name(len(first_matches)), "matches": first_matches})

    # Последующие пустые раунды.
    count = size // 2
    ri = 1
    while count > 1:
        count //= 2
        matches = [_empty_match(f"r{ri}m{i}") for i in range(count)]
        rounds.append({"name": _round_name(count), "matches": matches})
        ri += 1

    _propagate_byes(rounds)
    return rounds


def _propagate_byes(rounds: list[dict]) -> None:
    """Переносит победителей уже решённых матчей (байов) в следующий раунд."""
    for ri in range(len(rounds) - 1):
        for mi, m in enumerate(rounds[ri]["matches"]):
            if m["confirmed"] and m["winner"]:
                _place_winner(rounds, ri, mi, m["winner"])


def _place_winner(rounds: list[dict], ri: int, mi: int, winner: str) -> None:
    if ri + 1 >= len(rounds):
        return
    nxt = rounds[ri + 1]["matches"][mi // 2]
    if mi % 2 == 0:
        nxt["p1"] = winner
    else:
        nxt["p2"] = winner


def _serialize(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def list_tournaments() -> list[dict]:
    cur = tournaments_col.find().sort("created_at", -1)
    return [_serialize(t) for t in cur]


def get_tournament(tid: str) -> Optional[dict]:
    doc = _find(tid)
    return _serialize(doc) if doc else None


def _find(tid: str) -> Optional[dict]:
    doc = tournaments_col.find_one({"id": tid})
    if doc:
        return doc
    if ObjectId.is_valid(tid):
        return tournaments_col.find_one({"_id": ObjectId(tid)})
    return None


def create_tournament(name: str, game: str, prize: int, players: list[str]) -> dict:
    players = [p.strip() for p in players if p and p.strip()]
    if not name.strip():
        raise ValueError("Укажите название турнира")
    if len(players) < 2:
        raise ValueError("Нужно минимум 2 участника")

    doc = {
        "id": f"t{int(now_kz().timestamp() * 1000)}",
        "name": name.strip(),
        "game": game.strip() or "PS5",
        "prize": int(prize or 0),
        "status": "active",
        "players": players,
        "champion": None,
        "rounds": _build_bracket(players),
        "created_at": now_kz().isoformat(),
    }
    tournaments_col.insert_one(doc)
    return _serialize(doc)


def report_match(tid: str, match_id: str, score1: int, score2: int) -> dict:
    """Сохраняет счёт матча, определяет победителя и двигает сетку дальше."""
    doc = _find(tid)
    if not doc:
        raise ValueError("Турнир не найден")

    score1, score2 = int(score1), int(score2)
    if score1 == score2:
        raise ValueError("В матче на выбывание нужен победитель — счёт не может быть равным")

    rounds = doc["rounds"]
    found = None
    for ri, rnd in enumerate(rounds):
        for mi, m in enumerate(rnd["matches"]):
            if m["id"] == match_id:
                found = (ri, mi, m)
                break
        if found:
            break
    if not found:
        raise ValueError("Матч не найден")

    ri, mi, m = found
    if not m["p1"] or not m["p2"]:
        raise ValueError("Оба соперника ещё не определены")

    winner = m["p1"] if score1 > score2 else m["p2"]
    m["score1"], m["score2"] = score1, score2
    m["winner"], m["confirmed"] = winner, True

    if ri + 1 < len(rounds):
        _place_winner(rounds, ri, mi, winner)
        champion = None
    else:
        champion = winner  # это был финал

    update: dict[str, Any] = {"rounds": rounds}
    if champion:
        update["champion"] = champion
        update["status"] = "finished"
    tournaments_col.update_one({"_id": doc["_id"]}, {"$set": update})
    doc.update(update)
    return _serialize(doc)


def delete_tournament(tid: str) -> bool:
    doc = _find(tid)
    if not doc:
        return False
    tournaments_col.delete_one({"_id": doc["_id"]})
    return True
