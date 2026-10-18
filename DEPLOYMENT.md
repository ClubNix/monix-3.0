# Déploiement sécurisé de MONIX PROD

Le script `deploy-prod.sh` déploie uniquement le code depuis ce dossier vers
`nix-docker:/home/debian-nix/MONIX_PROD/app`.

Il ne copie, ne remplace et ne supprime jamais les données suivantes :

- `users.enc` et `key.key` ;
- `products.json` ;
- `static/uploads/` ;
- `logs/` ;
- `.monix-data.lock`, `.monix-pending-transaction.json` et `.flask-secret` ;
- `.env` et `secrets/`.

Les utilisateurs et produits de test restent donc locaux. Ceux de production
restent exclusivement sur le serveur.

## Première configuration

```bash
cd /home/ofwood/Documents/nix/monix
chmod +x deploy-prod.sh
./deploy-prod.sh --configure-smb
```

Le mot de passe n'est pas placé dans le projet. Il est enregistré avec des
permissions `600` dans `~/.config/monix-deploy/smb-credentials`.

## Vérification et simulation

```bash
./deploy-prod.sh --check
./deploy-prod.sh --dry-run
```

Pour tester ou créer uniquement une sauvegarde intégrale sans arrêter Docker :

```bash
./deploy-prod.sh --backup-only
```

## Déploiement

```bash
./deploy-prod.sh --execute
```

Séquence appliquée :

1. vérification de SSH, Docker, des données protégées et du partage SMB ;
2. copie intégrale et compressée de l'application distante ;
3. vérification SHA-256 locale et après transfert SMB ;
4. arrêt de `mendix_prod` ;
5. empreinte de toutes les données de production ;
6. envoi du code sans `--delete` et avec exclusions de sécurité ;
7. comparaison des données protégées avant/après ;
8. `docker compose up -d --build` ;
9. vérification de `http://127.0.0.1:5480/login` ;
10. restauration automatique de l'ancien code si le démarrage échoue.

Les valeurs peuvent être adaptées sans modifier le script :

```bash
MONIX_REMOTE=nix-docker \
MONIX_REMOTE_APP=/home/debian-nix/MONIX_PROD/app \
MONIX_SMB_SHARE=//100.126.181.100/data \
MONIX_SMB_BACKUP_DIR=nix/monix-prod-backups \
./deploy-prod.sh --check
```

Si une coupure brutale laisse un verrou distant après vérification qu'aucun
déploiement ne tourne encore :

```bash
./deploy-prod.sh --force-unlock
```
