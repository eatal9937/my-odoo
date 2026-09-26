#!/bin/bash
set -e

PASSWORD="32536933"

echo "=== 1. Stopping Odoo Web Container ==="
docker stop my-odoo-web-1

echo "=== 2. Backing up PostgreSQL Database (phongthep) ==="
# Dump db to /home/captain/my-odoo-backup.sql
docker exec -i my-odoo-db-1 pg_dump -U odoo -d phongthep > /home/captain/my-odoo-backup.sql

echo "=== 3. Stopping Odoo DB Container ==="
docker stop my-odoo-db-1

echo "=== 4. Archiving Odoo Configuration ==="
tar -czf /home/captain/my-odoo-config.tar.gz -C /home/captain/my-odoo/config .

echo "=== 5. Archiving Custom Addons ==="
tar -czf /home/captain/my-odoo-addons.tar.gz -C /home/captain/my-odoo/addons .

echo "=== 6. Archiving Odoo Filestore ==="
# Use sudo because files inside volume are root-owned
echo "$PASSWORD" | sudo -S tar -czf /home/captain/my-odoo-web-data.tar.gz -C /var/lib/docker/volumes/my-odoo_odoo-web-data/_data .

echo "=== Local backups completed successfully! ==="
ls -lh /home/captain/my-odoo-*
