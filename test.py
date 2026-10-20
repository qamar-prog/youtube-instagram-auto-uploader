import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise RuntimeError("Set GROQ_API_KEY in the root .env file before running this script.")

for model in Groq(api_key=api_key).models.list().data:
    print(model.id)
