"""Trending keyword miner: which words/phrases keep showing up in fast-growing GTA 6 titles.

Uses only the titles Scout already fetched (no extra API quota). Each title adds
log10(1 + views/hour) to every 1-3 word phrase in it (each phrase once per video), so a
phrase scores high when many fast videos use it. Phrases that start or end with a filler
word or with "gta"/"6"/etc. are skipped. The trend compares with the previous run's score.
"""
import math
import re

HASHTAG_RE = re.compile(r"#\S+")

BASE_WORDS = {"gta", "6", "vi", "gta6", "gtavi", "grand", "theft", "auto", "rockstar", "games",
              "shorts", "short", "video", "videos", "new", "official", "youtube"}
STOPWORDS = {
    # English
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "by",
    "from", "is", "are", "was", "were", "be", "been", "it", "its", "this", "that", "these",
    "those", "i", "you", "we", "they", "he", "she", "my", "your", "our", "their", "me", "us",
    "so", "if", "as", "just", "about", "all", "out", "up", "can", "will", "do", "does", "did",
    "not", "no", "yes", "vs", "what", "how", "why", "when", "who", "here", "there", "has",
    "have", "had", "get", "got", "into", "than", "then", "more", "most", "very", "ever",
    "ft", "feat", "part", "ep", "episode", "live", "stream", "day",
    # Hindi / Hinglish fillers
    "hai", "hain", "ka", "ki", "ke", "ko", "se", "me", "mein", "aur", "ye", "yeh", "wo", "woh",
    "kya", "kaise", "kyu", "kyun", "nahi", "bhi", "toh", "to", "ho", "hua", "gaya", "gya",
    "है", "का", "की", "के", "को", "से", "में", "और", "ये", "क्या", "नहीं", "भी",
}
def clean_title(title):
    return HASHTAG_RE.sub(" ", title or "")


WORD_RE = re.compile(r"(?:[^\W_]|[\u0900-\u097F])+(?:'[^\W_]+)?")


def tokens(title):
    return [w.lower() for w in WORD_RE.findall(clean_title(title))]


def phrases(title, max_n=3):
    """Distinct 1..max_n word phrases worth counting from one title."""
    ws = tokens(title)
    out = set()
    for n in range(1, max_n + 1):
        for i in range(len(ws) - n + 1):
            p = ws[i:i + n]
            if p[0] in STOPWORDS or p[-1] in STOPWORDS or p[0] in BASE_WORDS or p[-1] in BASE_WORDS:
                continue
            if all(w in BASE_WORDS or w in STOPWORDS or w.isdigit() for w in p):
                continue
            if n == 1 and (len(p[0]) < 3 or p[0].isdigit()):
                continue
            out.add(" ".join(p))
    return out


def mine(records, min_videos=2, top=40):
    """records: Scout rows (title, views_per_hour). -> [{phrase, score, videos, example_title}]"""
    score, count, example = {}, {}, {}
    for r in records:
        w = math.log10(1 + max(float(r.get("views_per_hour") or 0), 0))
        for p in phrases(r.get("title", "")):
            score[p] = score.get(p, 0) + w
            count[p] = count.get(p, 0) + 1
            if p not in example or w > example[p][0]:
                example[p] = (w, r.get("title", ""))
    rows = [{"phrase": p, "score": round(s, 2), "videos": count[p], "example_title": example[p][1]}
            for p, s in score.items() if count[p] >= min_videos]
    rows.sort(key=lambda r: (r["score"], len(r["phrase"])), reverse=True)
    # drop a short phrase when a longer one containing it scores almost the same
    kept = []
    for r in rows:
        if any(r["phrase"] in k["phrase"].split(" ") or f" {r['phrase']} " in f" {k['phrase']} "
               for k in kept if k["score"] >= 0.85 * r["score"] and k["phrase"] != r["phrase"]):
            continue
        kept.append(r)
        if len(kept) >= top:
            break
    return kept


def trend(score, prev_score):
    try:
        prev = float(prev_score)
    except (TypeError, ValueError):
        return "new"
    if prev <= 0:
        return "new"
    ratio = score / prev
    return "rising" if ratio >= 1.3 else "falling" if ratio <= 0.7 else "steady"
