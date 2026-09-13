'''
Author:     Sai Vignesh Golla
LinkedIn:   https://www.linkedin.com/in/saivigneshgolla/

Copyright (C) 2024 Sai Vignesh Golla

License:    GNU Affero General Public License
            https://www.gnu.org/licenses/agpl-3.0.en.html
            
GitHub:     https://github.com/GodsScion/Auto_job_applier_linkedIn

version:    24.12.3.10.30
'''


###################################################### CONFIGURE YOUR TOOLS HERE ######################################################

# Credentials are sourced from the encrypted secure-vault, never stored here.
# If the vault entries below are missing, run:
#   python "C:\Users\Admin\.agents\skills\secure-vault\vault.py" gui --request "linkedin-1:account"
#   python "C:\Users\Admin\.agents\skills\secure-vault\vault.py" gui --request "deepseek-llm-api:account"
import sys as _sys
_sys.path.insert(0, r"C:\Users\Admin\.agents\skills\secure-vault")
import vault as _vault

# Login Credentials for LinkedIn (Optional)
username = _vault.get_secret("linkedin-1", "email")
password = _vault.get_secret("linkedin-1", "password")
if not username or not password:
    raise RuntimeError(
        'Vault entry "linkedin-1" is missing email/password.\n'
        r'Run:  python "C:\Users\Admin\.agents\skills\secure-vault\vault.py" '
        'gui --request "linkedin-1:account"  and fill it in, then re-run.'
    )


## Artificial Intelligence (Beta Not-Recommended)
# Use AI
use_AI = False                          # True or False, Note: True or False are case-sensitive
'''
Note: Set it as True only if you want to use AI, and If you either have a
1. Local LLM model running on your local machine, with it's APIs exposed. Example softwares to achieve it are:
    a. Ollama - https://ollama.com/
    b. llama.cpp - https://github.com/ggerganov/llama.cpp
    c. LM Studio - https://lmstudio.ai/ (Recommended)
    d. Jan - https://jan.ai/
2. OR you have a valid OpenAI API Key, and money to spare, and you don't mind spending it.
CHECK THE OPENAI API PIRCES AT THEIR WEBSITE (https://openai.com/api/pricing/).
'''

##> ------ Yang Li : MARKYangL - Feature ------
# Select AI Provider
ai_provider = "deepseek"               # "openai", "deepseek"
'''
Note: Select your AI provider.
* "openai" - OpenAI API (GPT models)
* "deepseek" - DeepSeek API (DeepSeek models)
'''

# DeepSeek-compatible endpoint (non-secret)
llm_model = "deepseek-chat"
    # Examples: "deepseek-chat", "deepseek-reasoner"
##<

# Your Local LLM url or other AI api url and port (non-secret)
llm_api_url = "https://api.deepseek.com"       # Examples: "https://api.deepseek.com", "http://127.0.0.1:1234/v1/", "http://localhost:1234/v1/"
'''
Note: Don't forget to add / at the end of your url
'''

# Your Local LLM API key or other AI API key — sourced from the vault, never stored here.
# Default to empty string if not configured in vault so validator passes when use_AI is False.
llm_api_key = _vault.get_secret("deepseek-llm-api", "password") or ""
r'''
Note: If use_AI is True and this is empty, run:
  python "C:\Users\Admin\.agents\skills\secure-vault\vault.py" gui --request "deepseek-llm-api:account"
'''

#
llm_spec = "openai"                # Examples: "openai", "openai-like", "openai-like-github", "openai-like-mistral"
'''
Note: Currently "openai" and "openai-like" api endpoints are supported.
'''

# # Yor local embedding model name or other AI Embedding model name
# llm_embedding_model = "nomic-embed-text-v1.5"

# Do you want to stream AI output?
stream_output = False                    # Examples: True or False. (False is recommended for performance, True is recommended for user experience!)
'''
Set `stream_output = True` if you want to stream AI output or `stream_output = False` if not.
'''
##




############################################################################################################
'''
THANK YOU for using my tool 😊! Wishing you the best in your job hunt 🙌🏻!

Sharing is caring! If you found this tool helpful, please share it with your peers 🥺. Your support keeps this project alive.

Support my work on <PATREON_LINK>. Together, we can help more job seekers.

As an independent developer, I pour my heart and soul into creating tools like this, driven by the genuine desire to make a positive impact.

Your support, whether through donations big or small or simply spreading the word, means the world to me and helps keep this project alive and thriving.

Gratefully yours 🙏🏻,
Sai Vignesh Golla
'''
############################################################################################################