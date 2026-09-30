"""Council meeting video → a transcript the rest of the pipeline already understands.

지방의회는 회의를 인터넷방송으로 먼저 내보내고, 글로 된 회의록은 1~2주 뒤에 올립니다
(docs/real-data-clik.md). The video is the earliest record of a commitment, so this package turns
one meeting video into the same shape as a text minutes document — ``○도로과장 송태아 …`` speaker
lines — plus a timeline from character offsets back to seconds in the video:

    video ─ffmpeg→ 16 kHz mono audio ─silence-aligned windows→ STT per window ┐
      └─ffmpeg→ lower-third frames (scene changes + a slow tick) → OCR → speakers ┴→ transcript

Chunking, triage, extraction and the speaker check in grounding then run unchanged; evidence
spans map back to a time range through the timeline, so a clip of the answer can be cut.
Every step is checkpointed, so a job killed halfway through a two-hour meeting resumes from
the last finished window instead of paying for the audio again.
"""
