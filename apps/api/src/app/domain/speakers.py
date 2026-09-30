"""Executive-role vocabulary shared by parsing and evidence checks."""

# Unknown roles stay reviewable; an unrecognised speaker is never an executive by default.
OFFICIAL_ENDINGS = (
    "시장",
    "군수",
    "구청장",
    "국장",
    "과장",
    "실장",
    "팀장",
    "담당관",
    "소장",
    "본부장",
    "원장",
    "센터장",
    "관장",
    "사장",
    "대표이사",
)
