#!/usr/bin/env bash
set -Eeuo pipefail

IFS=$'\n\t'

LOCAL_APP="${MONIX_LOCAL_APP:-/home/ofwood/Documents/nix/monix}"
REMOTE="${MONIX_REMOTE:-nix-docker}"
REMOTE_APP="${MONIX_REMOTE_APP:-/home/debian-nix/MONIX_PROD/app}"
REMOTE_LOCK="${MONIX_REMOTE_LOCK:-/var/lock/monix-prod-deploy.lock}"
RSYNC_PATH="${MONIX_RSYNC_PATH:-sudo -n rsync}"

SMB_SHARE="${MONIX_SMB_SHARE:-//100.126.181.100/data}"
SMB_BACKUP_DIR="${MONIX_SMB_BACKUP_DIR:-nix/monix-prod-backups}"
SMB_CREDENTIALS="${MONIX_SMB_CREDENTIALS:-$HOME/.config/monix-deploy/smb-credentials}"

MODE="check"
ASSUME_YES=0
REMOTE_LOCK_HELD=0
DOCKER_STOPPED=0
DEPLOY_SUCCEEDED=0
REMOTE_ARCHIVE=""
WORK_DIR=""

PROTECTED_PATHS=(
    "/users.enc"
    "/key.key"
    "/products.json"
    "/static/uploads/"
    "/logs/"
    "/.monix-data.lock"
    "/.monix-pending-transaction.json"
    "/.flask-secret"
    "/.env"
    "/secrets/"
)

usage() {
    cat <<'EOF'
Usage:
  ./deploy-prod.sh --configure-smb
  ./deploy-prod.sh --check
  ./deploy-prod.sh --dry-run
  ./deploy-prod.sh --backup-only
  ./deploy-prod.sh --execute [--yes]
  ./deploy-prod.sh --force-unlock

--configure-smb  Enregistre l'utilisateur et le mot de passe SMB hors du projet.
--check          Vérifie SSH, Docker, les données protégées et l'accès SMB.
--dry-run        Affiche les fichiers de code qui seraient envoyés, sans modifier la prod.
--backup-only    Crée et vérifie une sauvegarde SMB sans arrêter Docker ni déployer.
--execute        Sauvegarde, arrête Docker, déploie le code, vérifie et redémarre.
--yes            Ne demande pas de saisir DEPLOYER (utile uniquement en automatisation).
--force-unlock   Supprime un verrou distant laissé par une exécution interrompue.
EOF
}

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
    printf 'ERREUR: %s\n' "$*" >&2
    exit 1
}

remote() {
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE" "$@"
}

compose() {
    remote "cd '$REMOTE_APP' && sudo -n docker compose $*"
}

configure_smb() {
    local username password domain credentials_dir
    credentials_dir=$(dirname "$SMB_CREDENTIALS")
    printf 'Utilisateur SMB [server] : '
    read -r username
    username=${username:-server}
    printf 'Mot de passe SMB : '
    read -rs password
    printf '\nDomaine SMB (laisser vide si aucun) : '
    read -r domain
    [[ -n "$password" ]] || die "le mot de passe SMB ne peut pas être vide"

    mkdir -p "$credentials_dir"
    chmod 700 "$credentials_dir"
    umask 077
    {
        printf 'username=%s\n' "$username"
        printf 'password=%s\n' "$password"
        [[ -z "$domain" ]] || printf 'domain=%s\n' "$domain"
    } > "$SMB_CREDENTIALS"
    chmod 600 "$SMB_CREDENTIALS"
    unset password
    log "Identifiants enregistrés dans $SMB_CREDENTIALS (mode 600)."
}

require_commands() {
    local command_name
    for command_name in ssh scp rsync smbclient tar gzip sha256sum flock curl; do
        command -v "$command_name" >/dev/null 2>&1 || die "commande locale absente : $command_name"
    done
}

check_local_source() {
    [[ -d "$LOCAL_APP" ]] || die "dossier local absent : $LOCAL_APP"
    local required
    for required in app.py Dockerfile docker-compose.yml requirements.txt templates; do
        [[ -e "$LOCAL_APP/$required" ]] || die "source incomplète : $LOCAL_APP/$required absent"
    done
}

check_remote() {
    remote "test -d '$REMOTE_APP' && test -f '$REMOTE_APP/docker-compose.yml'" \
        || die "application distante introuvable : $REMOTE:$REMOTE_APP"
    remote "test -s '$REMOTE_APP/users.enc' && test -s '$REMOTE_APP/key.key' && test -s '$REMOTE_APP/products.json'" \
        || die "une donnée de production obligatoire est absente ou vide"
    remote "command -v docker >/dev/null && command -v tar >/dev/null && command -v rsync >/dev/null && command -v flock >/dev/null && command -v curl >/dev/null" \
        || die "outils distants manquants"
    compose "config -q" >/dev/null
}

check_smb() {
    [[ -f "$SMB_CREDENTIALS" ]] || die "identifiants SMB absents ; lancez d'abord --configure-smb"
    local mode
    mode=$(stat -c '%a' "$SMB_CREDENTIALS")
    [[ "$mode" == "600" ]] || die "$SMB_CREDENTIALS doit être en mode 600 (actuel : $mode)"
    smbclient "$SMB_SHARE" -A "$SMB_CREDENTIALS" -c 'ls' >/dev/null \
        || die "connexion SMB impossible vers $SMB_SHARE"
}

ensure_smb_backup_dir() {
    local current="" part
    IFS='/' read -r -a parts <<< "$SMB_BACKUP_DIR"
    for part in "${parts[@]}"; do
        [[ -n "$part" ]] || continue
        current=${current:+$current/}$part
        smbclient "$SMB_SHARE" -A "$SMB_CREDENTIALS" -c "mkdir \"$current\"" >/dev/null 2>&1 || true
    done
    smbclient "$SMB_SHARE" -A "$SMB_CREDENTIALS" -c "cd \"$SMB_BACKUP_DIR\"; ls" >/dev/null \
        || die "dossier SMB inaccessible : $SMB_BACKUP_DIR"
}

rsync_args() {
    printf '%s\n' \
        -rlptz \
        --checksum \
        --itemize-changes \
        --human-readable \
        --no-owner \
        --no-group \
        "--rsync-path=$RSYNC_PATH" \
        --exclude=/users.enc \
        --exclude=/key.key \
        --exclude=/products.json \
        --exclude=/static/uploads/ \
        --exclude=/logs/ \
        --exclude=/.monix-data.lock \
        --exclude=/.monix-pending-transaction.json \
        --exclude=/.flask-secret \
        --exclude=/.env \
        --exclude=/secrets/ \
        --exclude=/__pycache__/ \
        --exclude='*.pyc' \
        --exclude=/.git/ \
        --exclude=/.gitignore \
        --exclude=/.gitattributes \
        --exclude=/.dockerignore \
        --exclude=/compose.dev.yml \
        --exclude=/dev-setup.sh \
        --exclude=/DEVELOPMENT.md \
        --exclude=/scripts/ \
        --exclude=/deploy-prod.sh \
        --exclude=/DEPLOYMENT.md
}

run_rsync() {
    local dry_run=${1:-0}
    local -a args
    mapfile -t args < <(rsync_args)
    if [[ "$dry_run" == "1" ]]; then
        args+=(--dry-run)
    fi
    # Intentionnellement aucun --delete : un déploiement ne supprime jamais un
    # fichier présent uniquement en production.
    rsync "${args[@]}" "$LOCAL_APP/" "$REMOTE:$REMOTE_APP/"
}

protected_manifest() {
    local output=$1
    remote "sudo -n bash -s -- '$REMOTE_APP'" > "$output" <<'REMOTE_SCRIPT'
set -euo pipefail
app=$1
cd "$app"
for path in users.enc key.key products.json .monix-pending-transaction.json .flask-secret .env; do
    if [[ -f "$path" ]]; then sha256sum -- "$path"; fi
done
for directory in static/uploads logs secrets; do
    if [[ -d "$directory" ]]; then
        find "$directory" -type f -print0 | sort -z | xargs -0r sha256sum
    fi
done
REMOTE_SCRIPT
}

create_backup() {
    local stamp archive_name remote_snapshot remote_checksum local_archive local_checksum verify_archive archive_listing
    stamp=$(date '+%Y%m%d-%H%M%S')
    archive_name="monix-prod_${stamp}.tar.gz"
    REMOTE_ARCHIVE="/var/tmp/$archive_name"
    remote_snapshot="/var/tmp/monix-prod-snapshot-$stamp"
    remote_checksum="$REMOTE_ARCHIVE.sha256"
    local_archive="$WORK_DIR/$archive_name"
    local_checksum="$local_archive.sha256"
    verify_archive="$WORK_DIR/verify-$archive_name"
    archive_listing="$WORK_DIR/$archive_name.list"

    log "Création d'un instantané intégral daté sur $REMOTE..."
    remote "sudo -n bash -s -- '$REMOTE_APP' '$remote_snapshot' '$REMOTE_ARCHIVE'" <<'REMOTE_SCRIPT'
set -euo pipefail
app=$1
snapshot=$2
archive=$3
rm -rf -- "$snapshot"
mkdir -p -- "$snapshot"
# Les écritures users/products utilisent ce verrou dans l'application.
flock -s "$app/.monix-data.lock" cp -a -- "$app" "$snapshot/app"
tar -C "$snapshot" -czf "$archive" app
chmod 600 "$archive"
(cd "$(dirname "$archive")" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256")
rm -rf -- "$snapshot"
REMOTE_SCRIPT

    scp -q "$REMOTE:$REMOTE_ARCHIVE" "$local_archive"
    scp -q "$REMOTE:$remote_checksum" "$local_checksum"
    (cd "$WORK_DIR" && sha256sum -c "$(basename "$local_checksum")") >/dev/null \
        || die "l'archive téléchargée ne correspond pas à l'archive distante"
    tar -tzf "$local_archive" > "$archive_listing"
    grep -Fxq 'app/users.enc' "$archive_listing" || die "users.enc absent de la sauvegarde"
    grep -Fxq 'app/key.key' "$archive_listing" || die "key.key absent de la sauvegarde"
    grep -Fxq 'app/products.json' "$archive_listing" || die "products.json absent de la sauvegarde"

    ensure_smb_backup_dir
    log "Envoi et vérification de $archive_name sur $SMB_SHARE/$SMB_BACKUP_DIR..."
    smbclient "$SMB_SHARE" -A "$SMB_CREDENTIALS" \
        -c "cd \"$SMB_BACKUP_DIR\"; put \"$local_archive\" \"$archive_name.part\"; get \"$archive_name.part\" \"$verify_archive\"" >/dev/null
    [[ "$(sha256sum "$local_archive" | awk '{print $1}')" == "$(sha256sum "$verify_archive" | awk '{print $1}')" ]] \
        || die "la vérification de la copie SMB a échoué"
    smbclient "$SMB_SHARE" -A "$SMB_CREDENTIALS" \
        -c "cd \"$SMB_BACKUP_DIR\"; rename \"$archive_name.part\" \"$archive_name\"; put \"$local_checksum\" \"$archive_name.sha256\"" >/dev/null
    log "Sauvegarde vérifiée : $SMB_SHARE/$SMB_BACKUP_DIR/$archive_name"
}

acquire_remote_lock() {
    remote "sudo -n mkdir '$REMOTE_LOCK'" \
        || die "un autre déploiement est en cours (verrou : $REMOTE:$REMOTE_LOCK)"
    REMOTE_LOCK_HELD=1
    remote "printf '%s\n' '$(date -Is) $(hostname) $$' | sudo -n tee '$REMOTE_LOCK/owner' >/dev/null"
}

release_remote_lock() {
    if [[ "$REMOTE_LOCK_HELD" == "1" ]]; then
        remote "sudo -n rm -f '$REMOTE_LOCK/owner'; sudo -n rmdir '$REMOTE_LOCK'" >/dev/null 2>&1 || true
        REMOTE_LOCK_HELD=0
    fi
}

rollback_code() {
    [[ -n "$REMOTE_ARCHIVE" ]] || return 0
    log "Échec détecté : restauration automatique de l'ancien code (données de prod conservées)..."
    remote "sudo -n bash -s -- '$REMOTE_APP' '$REMOTE_ARCHIVE'" <<'REMOTE_SCRIPT'
set -euo pipefail
app=$1
archive=$2
rollback=$(mktemp -d /var/tmp/monix-rollback.XXXXXX)
trap 'rm -rf -- "$rollback"' EXIT
tar -xzf "$archive" -C "$rollback"
rsync -a \
  --exclude=/users.enc --exclude=/key.key --exclude=/products.json \
  --exclude=/static/uploads/ --exclude=/logs/ \
  --exclude=/.monix-data.lock --exclude=/.monix-pending-transaction.json \
  --exclude=/.flask-secret \
  --exclude=/.env --exclude=/secrets/ \
  "$rollback/app/" "$app/"
cd "$app"
docker compose up -d --build
REMOTE_SCRIPT
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if [[ "$status" -ne 0 && "$DOCKER_STOPPED" == "1" && "$DEPLOY_SUCCEEDED" != "1" ]]; then
        rollback_code || printf 'ERREUR: restauration automatique impossible ; archive distante : %s\n' "$REMOTE_ARCHIVE" >&2
    fi
    if [[ "$DEPLOY_SUCCEEDED" == "1" && -n "$REMOTE_ARCHIVE" ]]; then
        remote "sudo -n rm -f '$REMOTE_ARCHIVE' '$REMOTE_ARCHIVE.sha256'" >/dev/null 2>&1 || true
    fi
    release_remote_lock
    [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
    exit "$status"
}

health_check() {
    local attempt
    for attempt in $(seq 1 30); do
        if remote "curl -fsS --max-time 4 http://127.0.0.1:5480/login >/dev/null"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

execute_deploy() {
    local manifest_before manifest_after
    if [[ "$ASSUME_YES" != "1" ]]; then
        printf 'Cette opération va sauvegarder puis redémarrer MONIX PROD sur %s.\n' "$REMOTE"
        printf 'Saisissez DEPLOYER pour continuer : '
        local confirmation
        read -r confirmation
        [[ "$confirmation" == "DEPLOYER" ]] || die "déploiement annulé"
    fi

    WORK_DIR=$(mktemp -d /tmp/monix-deploy.XXXXXX)
    chmod 700 "$WORK_DIR"
    trap cleanup EXIT INT TERM
    mkdir -p "$HOME/.cache"
    exec 9>"$HOME/.cache/monix-prod-deploy.lock"
    flock -n 9 || die "un déploiement local est déjà en cours"
    acquire_remote_lock
    create_backup

    manifest_before="$WORK_DIR/protected-before.sha256"
    manifest_after="$WORK_DIR/protected-after.sha256"
    log "Arrêt du conteneur de production..."
    compose "stop --timeout 30"
    DOCKER_STOPPED=1
    protected_manifest "$manifest_before"

    log "Envoi du nouveau code sans aucune suppression et sans données de test..."
    run_rsync 0
    protected_manifest "$manifest_after"
    cmp -s "$manifest_before" "$manifest_after" \
        || die "une donnée protégée a changé ; déploiement interrompu"

    log "Reconstruction et redémarrage du conteneur..."
    compose "up -d --build"
    health_check || die "l'application ne répond pas sur http://127.0.0.1:5480/login"

    DEPLOY_SUCCEEDED=1
    DOCKER_STOPPED=0
    compose "ps"
    log "Déploiement terminé. Les utilisateurs, produits, uploads et logs de production sont inchangés."
}

backup_only() {
    WORK_DIR=$(mktemp -d /tmp/monix-backup.XXXXXX)
    chmod 700 "$WORK_DIR"
    trap cleanup EXIT INT TERM
    mkdir -p "$HOME/.cache"
    exec 9>"$HOME/.cache/monix-prod-deploy.lock"
    flock -n 9 || die "une sauvegarde ou un déploiement local est déjà en cours"
    acquire_remote_lock
    create_backup
    # Réutilise le nettoyage de fin pour supprimer l'archive temporaire du
    # serveur. La copie vérifiée sur le SMB est bien entendu conservée.
    DEPLOY_SUCCEEDED=1
    log "Sauvegarde seule terminée ; Docker n'a pas été arrêté."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --configure-smb) MODE="configure" ;;
        --check) MODE="check" ;;
        --dry-run) MODE="dry-run" ;;
        --backup-only) MODE="backup-only" ;;
        --execute) MODE="execute" ;;
        --yes) ASSUME_YES=1 ;;
        --force-unlock) MODE="force-unlock" ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "option inconnue : $1" ;;
    esac
    shift
done

case "$MODE" in
    configure)
        configure_smb
        ;;
    force-unlock)
        remote "sudo -n rm -rf '$REMOTE_LOCK'"
        log "Verrou distant supprimé."
        ;;
    check|dry-run|backup-only|execute)
        require_commands
        check_local_source
        check_remote
        check_smb
        if [[ "$MODE" == "check" ]]; then
            log "Préflight réussi : SSH, Docker, données protégées et SMB sont accessibles."
        elif [[ "$MODE" == "dry-run" ]]; then
            log "Simulation rsync (aucune modification distante)..."
            run_rsync 1
            log "Simulation terminée."
        elif [[ "$MODE" == "backup-only" ]]; then
            backup_only
        else
            execute_deploy
        fi
        ;;
esac
