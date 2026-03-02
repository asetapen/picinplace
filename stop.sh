#!/usr/bin/env bash

systemctl --user stop picinplace.service
systemctl --user disable picinplace.service
systemctl --user daemon-reload

