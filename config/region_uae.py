"""Rules for the LinkedIn UAE pass (linkedin_uae_overlay.py). Search settings and job filters only: no answers live here
(every answer comes from the closed-world facts file outside the repo)."""

# Data Platform identity first; ML/AI titles belong to the later AI Engineer identity.
SEARCH_TERMS = [
    "Data Engineer",
    "Data Platform Engineer",
    "Big Data Engineer",
    "AWS Data Engineer",
    "Snowflake Data Engineer",
    "ETL Developer",
    "PySpark Developer",
    "Analytics Engineer",
    "Data Pipeline Engineer",
    "Cloud Data Engineer",
    "Data Warehouse Engineer",
    "DataOps Engineer",
    "Python Data Engineer",
    "Spark Developer",
]

SEARCH_LOCATION = "United Arab Emirates"
EXPERIENCE_LEVEL = ["Associate", "Mid-Senior level"]
JOB_TYPE = ["Full-time"]
DATE_POSTED = "Past week"
SORT_BY = "Most recent"
SWITCH_NUMBER = 8          # jobs visited per search term (inventory: a visit budget; live: also capped by the daily quota)
DAILY_QUOTA = 12           # live applications a day in the UAE pass; the India pass gets what LinkedIn's shared limit leaves

# A job card's location must look like the UAE, or the job is skipped (a "United Arab Emirates" search can return remote roles elsewhere).
UAE_LOCATION_WORDS = ("uae", "united arab emirates", "dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah", "fujairah",
                      "umm al quwain", "al ain", "emirates")

# Added to the India bad_words for this pass. Substring match on the lower-cased job description, so each phrase says
# "this job needs something the user does not have": Arabic as a requirement (not "a plus"), UAE nationals, people already
# in the UAE, a visa of one's own, or "no sponsorship". The user needs employer sponsorship and is in India.
BAD_WORDS_EXTRA = [
    "arabic speaking", "arabic-speaking", "fluent in arabic", "fluency in arabic", "arabic fluency", "native arabic",
    "arabic is a must", "arabic is required", "arabic is mandatory", "arabic (mandatory)", "arabic language is required",
    "uae national", "emirati national", "emiratis only", "emirati only", "gcc national", "gcc nationals", "uae nationals",
    "local candidates only", "uae residents only", "residents only", "must be based in the uae", "must be in the uae",
    "must be based in dubai", "currently based in the uae", "currently in the uae", "candidates based in the uae",
    "uae-based candidates only", "valid uae visa", "uae visa holders only", "visa holders only", "own visa", "transferable visa",
    "visa transfer", "employment visa is not provided", "no visa sponsorship", "does not provide visa", "unable to sponsor",
    "sponsorship is not available", "will not sponsor", "not able to sponsor", "we cannot sponsor", "no sponsorship",
    "uae driving licence required", "uae driving license required",
]
