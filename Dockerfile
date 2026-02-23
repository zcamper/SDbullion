# Specify the parent image from which we build
FROM apify/actor-python-playwright:3.10

# Copy all files to the container
COPY . ./

# Install dependencies from requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Run the command to start the actor
CMD ["python3", "main.py"]
