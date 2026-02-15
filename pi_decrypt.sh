#!/bin/bash
# Decrypt all .enc files in /opt/yarbo-bridge
set -e
cd /opt/yarbo-bridge
KEY=$(cat .encryption_key)
for enc_file in $(find . -name "*.enc" -not -path "*/.git/*"); do
    plain_file="${enc_file%.enc}"
    openssl enc -aes-256-cbc -d -salt -pass "pass:$KEY" -in "$enc_file" -out "$plain_file"
    echo "  decrypted: $enc_file -> $plain_file"
done
echo "DECRYPT_OK"
