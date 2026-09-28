# Mobile Grooming Planner v4

New in v4:
- Natural-language-style planner input
- Parses groomer, area, time, and common duration phrases
- Optional automatic address geocoding
- Optional real road distance and drive-time scoring through Google Maps
- Scores candidates around appointments before/after a schedule opening
- Tracks Last Contacted so recently messaged clients can be penalized
- Works without an API key in basic mode

Run:
pip install -r requirements.txt
streamlit run app.py

Google Maps:
Add a Streamlit secret or environment variable named GOOGLE_MAPS_API_KEY.

Do not commit a real API key into GitHub.

Current limitation:
The chat box is a lightweight rule-based parser, not an LLM yet.
That keeps this version simple and cheap to run.
