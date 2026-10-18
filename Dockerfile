# Utiliser l'image officielle Python 3.12.9 basée sur Debian Bookworm
FROM python:3.12.9-bookworm

# Définir le répertoire de travail dans le conteneur
WORKDIR /app

# Copier les fichiers nécessaires
COPY requirements.txt .

# Installer les dépendances
RUN pip install --no-cache-dir -r requirements.txt

# Copier tout le code source
COPY . .

# Définir la commande de démarrage

CMD ["python3", "app.py"]

# CMD ["python3", "-m", "gunicorn", "-w", "5", "-k", "gthread", "--threads", "2", "--timeout", "60", "-b", "0.0.0.0:8080", "app:app"]

# CMD ["python3", "app.py"]
# CMD ["python3", "-m", "gunicorn", "-b", "0.0.0.0:8080", "app:app"]
