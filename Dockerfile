# Use official Python base image
FROM python:3.11-slim-bookworm

# Hugging Face Spaces runs the container as user id 1000 with no write access
# to the root filesystem. Create that user up front.
RUN useradd -m -u 1000 user

# Install browsers to a shared, world-readable path so the non-root runtime
# user can find Chromium (Playwright defaults to the user's home cache).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (as root -> system site-packages, readable by all)
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + its system libs, then make the browser dir
# readable/executable by the unprivileged runtime user.
RUN playwright install --with-deps chromium \
    && chmod -R 777 /ms-playwright

# Copy application files owned by the runtime user
COPY --chown=user . /app

# Ensure the downloads dir exists and is writable by the runtime user
RUN mkdir -p /app/downloads && chown -R user:user /app

# Switch to the non-root user expected by Hugging Face Spaces
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Hugging Face routes traffic to the port declared as app_port in README.md.
ENV PORT=7860
EXPOSE 7860

# Use shell form to ensure proper variable expansion
CMD ["/bin/bash", "/app/entrypoint.sh"]
