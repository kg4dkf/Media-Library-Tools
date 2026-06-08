"""Auto-categorize depth-1 folders. Loads all files into memory once - fast."""
import csv, re, sqlite3
from collections import defaultdict, Counter

DB = "/tmp/music_snapshot.db"
OUT = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_depth1_categorization.csv"

EXACT = {
    "soundtracks": "SOUNDTRACK", "soundtrack": "SOUNDTRACK", "ost": "SOUNDTRACK",
    "compilations": "COMPILATION", "various artists": "COMPILATION",
    "various": "COMPILATION", "va": "COMPILATION",
    "wvum": "RADIO",
    "sound effects": "SOUNDEFFECTS",
    "video game sounds": "VIDEOGAME", "video game soundtracks": "VIDEOGAME",
    "audiobooks": "AUDIOBOOK", "audio books": "AUDIOBOOK",
    "comedy": "COMEDY", "stand-up": "COMEDY", "stand up comedy": "COMEDY",
}

GENRE_BUCKETS = {
    "90s","80s","70s","60s","50s","00s","10s","oldies",
    "country","pop","rock","classical","jazz","folk","blues","reggae",
    "hip hop","hip-hop","rap","r&b","rnb","soul","disco","metal",
    "punk","alternative","indie","electronic","dance","techno","house",
    "ambient","new age","world","latin","spanish","italian","french",
    "german","japanese","korean","chinese","arabic",
    "irish","gaelic","celtic","christmas","halloween","holiday",
    "musicals","musical",
}

NAME_HINTS = [
    (re.compile(r"\b(audiobook|audio[ -]?book|lecture|lectures)\b", re.I), "AUDIOBOOK", "medium"),
    (re.compile(r"\b(comedy|comedian|stand[- ]?up|jack benny show|monologue)\b", re.I), "COMEDY", "medium"),
    (re.compile(r"\b(soundtrack|score|original motion picture|cinema|film music)\b", re.I), "SOUNDTRACK", "medium"),
    (re.compile(r"\b(playlist|top \d+|best of \d+|greatest songs|hits of)\b", re.I), "COMPILATION", "medium"),
    (re.compile(r"\b(discography|complete)\b", re.I), "ARTIST", "medium"),
    (re.compile(r"\b(radio|wvum|alumni show)\b", re.I), "RADIO", "medium"),
    (re.compile(r"\b(sound effects|sfx)\b", re.I), "SOUNDEFFECTS", "medium"),
    (re.compile(r"\b(video game|videogame|game music|game ost|nintendo|gameboy)\b", re.I), "VIDEOGAME", "medium"),
]

VA_NAMES = {"various artists","various","va"}


def classify(folder_name, subtree_audio, distinct_artists, dominant_pct, dominant_name):
    nlower = folder_name.strip().lower()
    if nlower in EXACT:
        return EXACT[nlower], "high", "exact name match"
    if nlower in GENRE_BUCKETS:
        return "DISSOLVE", "high", "genre/era bucket"
    for pat, cat, conf in NAME_HINTS:
        if pat.search(folder_name):
            return cat, conf, "name-pattern"
    if subtree_audio == 0:
        return "REVIEW", "low", "no audio in subtree"
    if distinct_artists == 0 or not dominant_name:
        return "REVIEW", "low", "audio present but untagged"
    if distinct_artists == 1 and dominant_name.lower() in VA_NAMES:
        return "COMPILATION", "high", "Various Artists tag"
    if distinct_artists == 1:
        return "ARTIST", "high", "single artist: " + dominant_name
    if distinct_artists <= 3 and dominant_pct >= 0.7:
        return "ARTIST", "high", "dominant {:.0%}".format(dominant_pct)
    if dominant_pct >= 0.6:
        return "ARTIST", "medium", "dominant {:.0%}".format(dominant_pct)
    if distinct_artists >= 20 and dominant_pct < 0.2:
        return "COMPILATION", "high", "{} artists no dominant".format(distinct_artists)
    if distinct_artists >= 8 and dominant_pct < 0.4:
        return "COMPILATION", "medium", "{} artists dom {:.0%}".format(distinct_artists, dominant_pct)
    return "REVIEW", "low", "{} artists dom {:.0%}".format(distinct_artists, dominant_pct)


def top1(rel_path):
    """Return the first segment of rel_path."""
    for sep in ("/", chr(92)):
        i = rel_path.find(sep)
        if i >= 0:
            return rel_path[:i]
    return rel_path


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    folders = list(con.execute(
        "SELECT folder_name, audio_file_count, subfolder_count, total_size_bytes "
        "FROM folders WHERE depth=1 ORDER BY total_size_bytes DESC"
    ))

    # Pre-aggregate: top1 -> Counter(tag_artist) for audio files
    print("aggregating artist counts per top-level folder...")
    artist_by_top = defaultdict(Counter)
    audio_by_top = Counter()
    tagged_by_top = Counter()
    for row in con.execute(
        "SELECT rel_path, tag_artist FROM files WHERE is_audio='True' AND error=''"
    ):
        t = top1(row["rel_path"])
        audio_by_top[t] += 1
        a = row["tag_artist"]
        if a:
            tagged_by_top[t] += 1
            artist_by_top[t][a] += 1
    print("aggregation done. tops:", len(audio_by_top))

    rows_out = []
    for fld in folders:
        name = fld["folder_name"]
        ac = artist_by_top.get(name, Counter())
        subtree_audio = audio_by_top.get(name, 0)
        tagged = tagged_by_top.get(name, 0)
        distinct_a = len(ac)
        if ac and tagged > 0:
            top_artist, top_count = ac.most_common(1)[0]
            dominant_pct = top_count / tagged
        else:
            top_artist = ""
            dominant_pct = 0.0
        cat, conf, reason = classify(name, subtree_audio, distinct_a, dominant_pct, top_artist)
        top5 = "; ".join("{}({})".format(a, c) for a, c in ac.most_common(5))
        rows_out.append([
            name, subtree_audio, fld["subfolder_count"],
            round(fld["total_size_bytes"]/(1024**3), 2),
            distinct_a, top_artist, round(dominant_pct*100, 1),
            top5, cat, conf, reason, ""
        ])

    cols = ["folder_name","subtree_audio_files","subfolders","size_gb",
            "distinct_artists_in_subtree","dominant_artist","dominant_pct",
            "top_5_artists","auto_category","confidence","reason","user_override"]
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows_out)

    by_cat = Counter(r[8] for r in rows_out)
    by_conf = Counter(r[9] for r in rows_out)
    print("Total:", len(rows_out))
    print("By category:")
    for k, n in by_cat.most_common():
        print(" ", k, n)
    print("By confidence:")
    for k, n in by_conf.most_common():
        print(" ", k, n)
    con.close()

if __name__ == "__main__":
    main()
