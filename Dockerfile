FROM python:3.12-slim

WORKDIR /app

# System deps for lxml/bs4 parsing are already covered by slim + pip wheels
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Both files must be present: the agent + the worker that drives it
COPY deanonymize_employer.py worker.py ./

# Long-running worker, not a web server — no EXPOSE / port needed
CMD ["python", "worker.py"]
