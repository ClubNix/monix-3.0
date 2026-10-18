# Développement local

Cet environnement fonctionne uniquement sur le laptop. Il utilise le conteneur
`monix_dev`, le port local `5481` et le volume Docker `monix_dev_data`. Il ne se
connecte pas au serveur de production.

## Démarrage et initialisation

```bash
./dev-setup.sh
```

Ouvrir ensuite <http://127.0.0.1:5481>.

- utilisateur : `user` / `user`
- administrateur : `admin` / `admin`

Le code du projet est monté dans le conteneur. Après une modification Python,
redémarrer l'application avec :

```bash
docker compose -f compose.dev.yml restart monix_dev
```

Commandes utiles :

```bash
docker compose -f compose.dev.yml logs -f monix_dev
docker compose -f compose.dev.yml stop monix_dev
docker compose -f compose.dev.yml up -d monix_dev
```

Pour remettre uniquement les deux comptes de test à leurs valeurs initiales :

```bash
docker compose -f compose.dev.yml run --rm --no-deps monix_dev python3 scripts/init-dev-users.py
```

Le volume `monix_dev_data` conserve les données entre les redémarrages. Sa
suppression efface définitivement toutes les données locales de test ; elle
n'est donc pas effectuée par les scripts.
