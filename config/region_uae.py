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

# A job's TITLE must name a data-engineering role the user would actually take, or the job is not even opened. LinkedIn pads a "Data Engineer"
# search with unrelated jobs: the first real pass's answerable list held a nurse, an administrator and a perfume-shop Shopify developer.
# The title is normalised first (Unicode NFKC, so a recruiter's mathematical-bold "Data Engineer" reads as plain text; lower case; hyphens, slashes,
# dots and non-breaking spaces become spaces; plural "engineers" counts). A title passes when it holds a strong phrase (TITLE_ANY), matches a pattern
# (TITLE_PATTERNS), or holds a bare technology word (TITLE_TECH) together with a role word (TITLE_ROLE): "Spark Developer" yes, "Spark Plug Mechanic" no.
# "Data Analyst", "Data Scientist", "Platform Engineer" (DevOps) and "Technology Lead" match nothing on purpose. AI / ML engineer titles are NOT here:
# they belong to the later AI Engineer identity (the resume this pass sends is the Data Platform one); MLOps is a platform role and stays.
TITLE_ANY = (
    "data engineer", "data engineering", "data platform", "data warehouse", "data warehousing", "data lake", "data lakehouse", "lakehouse engineer",
    "data pipeline", "data infrastructure", "data integration", "data developer", "big data", "analytics engineer",
    "analytic engineer", "cloud data", "dataops", "mlops", "bi engineer", "bi engineering", "business intelligence engineer", "sql developer", "database developer", "database engineer", "azure data factory", "hadoop developer",
    "hadoop engineer",
)
TITLE_PATTERNS = (
    r"\bdata\b[^,;()]{0,25}\bengineer(s|ing)?\b",       # Data Platform Engineer, Data Quality Engineer, Data Lineage Engineer, Data Operations Engineer
)
TITLE_TECH = ("etl", "elt", "dwh", "spark", "pyspark", "airflow", "dbt", "kafka", "snowflake", "databricks", "hadoop", "informatica", "ssis", "talend")
TITLE_ROLE = ("engineer", "engineers", "developer", "developers", "consultant", "specialist", "programmer")
# Words that rule a job out even when a data word is in the title (the user has about three years of experience and is not looking for these).
# Kept short on purpose: anything else that is not a data role is already refused by the lists above ("Data Engineer (Sales Analytics)" is fine).
# U3 (2026-10-17, his decision D1 of 2026-10-08): leadership and architect titles are out too: "Lead Data Engineer", "Staff Data Engineer",
# "Principal Data Engineer", "Head of Data", "Data Engineering Manager", "Data Architect", "Director of Data", "VP Data", "Chief Data Officer".
# Whole words only (the title gate matches \bword(s)?\b): "leadership" and "headquarters" do not match "lead" and "head".
TITLE_NONE = ("intern", "internship", "trainee", "apprentice", "fresher", "graduate programme", "graduate program", "scientist",
              "lead", "staff", "principal", "head", "manager", "architect", "director", "vp", "chief",
              # the same kind of seniority written another way (found in review of the real titles): "Team Leader", "Supervisor", "SVP", "CTO" ...
              "leader", "leadership", "supervisor", "svp", "avp", "cto", "cdo", "president")

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
