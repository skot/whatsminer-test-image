#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Usage:
  tools/build_access_image.sh \
    --stock-img artifacts/images/stock-h6os-20220422.18.img \
    --dump-dir path/to/openix-dump \
    --output artifacts/images/h6os-access.img

By default this embeds the repository's static test key. Optionally set
SSH_PUBKEY_FILE to embed a different public key.

The dump directory must contain at least:
  boot0_sdcard.fex
  boot_package.fex
  sunxi_mbr.fex
  data.fex
EOF
}

stock_img=
dump_dir=
output_img=
ssh_pubkey_file=${SSH_PUBKEY_FILE:-}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--stock-img)
			stock_img=$2
			shift 2
			;;
		--dump-dir)
			dump_dir=$2
			shift 2
			;;
		--output)
			output_img=$2
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "Unknown argument: $1" >&2
			usage >&2
			exit 2
			;;
	esac
done

if [ -z "$stock_img" ] || [ -z "$dump_dir" ] || [ -z "$output_img" ]; then
	usage >&2
	exit 2
fi

if [ -z "$ssh_pubkey_file" ]; then
	ssh_pubkey_file=keys/whatsminer_test_rescue_key.pub
fi

for path in "$stock_img" "$dump_dir/boot0_sdcard.fex" "$dump_dir/boot_package.fex" "$dump_dir/sunxi_mbr.fex" "$dump_dir/data.fex" "$ssh_pubkey_file"; do
	if [ ! -e "$path" ]; then
		echo "Missing required path: $path" >&2
		exit 1
	fi
done
if [ ! -f payload/inittab ] || [ ! -f payload/serial-shell.sh.in ]; then
	echo "Missing payload templates under payload/" >&2
	exit 1
fi

work_dir=${WORK_DIR:-work-data/access-build}
payload_img="$work_dir/data-access.fex"
serial_shell="$work_dir/serial-shell.sh"
patched_phoenix="$work_dir/patched-phoenix.img"

mkdir -p "$work_dir" "$(dirname "$output_img")"
cp "$dump_dir/data.fex" "$payload_img"

ssh_key=$(sed -n '1p' "$ssh_pubkey_file")
case "$ssh_key" in
	ssh-*|ecdsa-*|sk-*) ;;
	*)
		echo "SSH_PUBKEY_FILE does not look like an SSH public key: $ssh_pubkey_file" >&2
		exit 1
		;;
esac

sed "s#__AUTHORIZED_KEY__#$ssh_key#" payload/serial-shell.sh.in > "$serial_shell"
chmod 755 "$serial_shell"

docker_image=${EXT4_TOOLS_IMAGE:-whatsminer-ext4-tools}
if ! docker image inspect "$docker_image" >/dev/null 2>&1; then
	docker build -t "$docker_image" docker/ext4-tools
fi

docker run --rm -v "$PWD:/work" -w /work "$docker_image" sh -lc "
	set -e
	debugfs -w -R 'rm /etc/serial-shell.sh' '$payload_img' >/dev/null 2>&1 || true
	debugfs -w -R 'rm /etc/inittab' '$payload_img' >/dev/null 2>&1 || true
	debugfs -w -R 'write $serial_shell /etc/serial-shell.sh' '$payload_img'
	debugfs -w -R 'write payload/inittab /etc/inittab' '$payload_img'
	debugfs -w -R 'sif /etc/serial-shell.sh mode 0100755' '$payload_img'
	debugfs -w -R 'sif /etc/inittab mode 0100644' '$payload_img'
	e2fsck -fy '$payload_img'
"

python3 tools/patch_phoenix_data_payload.py "$stock_img" "$payload_img" "$patched_phoenix"
python3 tools/build_phoenix_product_force_sprite.py "$patched_phoenix" "$dump_dir" "$output_img"

cat <<EOF

Access image written to:
  $output_img

Embedded public key:
  $ssh_pubkey_file

SSH command after flashing and NAND boot:
  ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa -o IdentitiesOnly=yes -i ${ssh_pubkey_file%.pub} micro@192.168.1.222
EOF
