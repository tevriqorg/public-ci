#!/bin/sh
set -u

API=${HOME_VISION_NORMALIZE_API:-http://192.168.50.61:8000/internal/normalize}
SMB_ROOT=${HOME_VISION_SMB_ROOT:-/share/CACHEDEV1_DATA/home-vision-a8}
WORK_ROOT=${HOME_VISION_WORK_ROOT:-/share/CACHEDEV1_DATA/home-vision-qnap-worker}
DOCKER=${HOME_VISION_DOCKER:-/share/CACHEDEV1_DATA/.qpkg/container-station/usr/bin/.libs/docker}
IMAGE=${HOME_VISION_FFMPEG_IMAGE:-jrottenberg/ffmpeg:6.1-ubuntu}
WORKERS=${HOME_VISION_WORKERS:-3}

json_string() {
    key=$1
    json=$2
    printf '%s' "$json" | sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

json_number() {
    key=$1
    json=$2
    printf '%s' "$json" | sed -n "s/.*\"$key\"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p"
}

post() {
    path=$1
    body=$2
    curl -fsS --connect-timeout 8 --max-time 40 \
        -H 'Content-Type: application/json' \
        -d "$body" "$API/$path"
}

log() {
    printf '%s worker=%s asset=%s phase=%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$worker_id" "${asset_id:--}" "$1" "${2:-}"
}

validate_output() {
    mount_root=$1
    candidate_path=$2
    output_duration=$(
        "$DOCKER" run --rm --user 0:0 \
            -v "$mount_root:/work:ro" \
            --entrypoint ffprobe "$IMAGE" \
            -v error -show_entries format=duration -of default=nw=1:nk=1 \
            "$candidate_path" 2>/dev/null
    ) || return 1
    output_seconds=$(printf '%s\n' "$output_duration" | cut -d. -f1)
    expected_seconds=$((duration_ms / 1000))
    [ -n "$output_seconds" ] || return 1
    duration_delta=$((output_seconds - expected_seconds))
    [ "$duration_delta" -ge 0 ] || duration_delta=$((-duration_delta))
    allowed_seconds=$((duration_ms * 2 / 100000))
    [ "$allowed_seconds" -ge 5 ] || allowed_seconds=5
    [ "$allowed_seconds" -le 120 ] || allowed_seconds=120
    [ "$duration_delta" -le "$allowed_seconds" ] || return 1

    video_profile=$(
        "$DOCKER" run --rm --user 0:0 \
            -v "$WORK_ROOT:/work:ro" \
            --entrypoint ffprobe "$IMAGE" \
            -v error -select_streams v:0 \
            -show_entries stream=codec_name,codec_tag_string -of csv=p=0 \
            "$candidate_path" 2>/dev/null
    ) || return 1
    [ "$video_profile" = "hevc,hvc1" ] || return 1

    if [ "$audio_codec" = pcm_alaw ]; then
        audio_profile=$(
            "$DOCKER" run --rm --user 0:0 \
                -v "$WORK_ROOT:/work:ro" \
                --entrypoint ffprobe "$IMAGE" \
                -v error -select_streams a:0 \
                -show_entries stream=codec_name,sample_rate,channels -of csv=p=0 \
            "$candidate_path" 2>/dev/null
        ) || return 1
        [ "$audio_profile" = "aac,8000,1" ] || return 1
    elif [ "$audio_codec" = aac ]; then
        audio_profile=$(
            "$DOCKER" run --rm --user 0:0 \
                -v "$WORK_ROOT:/work:ro" \
                --entrypoint ffprobe "$IMAGE" \
                -v error -select_streams a:0 -show_entries stream=codec_name \
                -of csv=p=0 "/work/$asset_id.mp4.part" 2>/dev/null
        ) || return 1
        [ "$audio_profile" = "aac" ] || return 1
    fi

    mid_sec=$((duration_ms / 2000))
    end_sec=$((duration_ms / 1000 - 1))
    [ "$end_sec" -ge 0 ] || end_sec=0
    for offset in 0 "$mid_sec" "$end_sec"; do
        if ! "$DOCKER" run --rm --user 0:0 \
                -v "$mount_root:/work:ro" \
                "$IMAGE" -v error -fflags +genpts+discardcorrupt \
            -err_detect ignore_err -i "$candidate_path" \
            -ss "$offset" -an -frames:v 1 -f null - >/dev/null; then
            return 1
        fi
    done
    return 0
}

valid_relative_path() {
    case "$1" in
        ""|/*|*..*) return 1 ;;
    esac
    return 0
}

worker() {
    worker_id=$1
    while :; do
        claim_json=$(post claim "{\"worker_id\":\"$worker_id\"}" 2>/dev/null || true)
        case "$claim_json" in
            *'"claim":null'*) sleep 20; continue ;;
            "") sleep 20; continue ;;
        esac

        asset_id=$(json_string asset_id "$claim_json")
        source_rel=$(json_string source_relative_path "$claim_json")
        output_rel=$(json_string output_relative_path "$claim_json")
        audio_codec=$(json_string source_audio_codec "$claim_json")
        duration_ms=$(json_number duration_ms "$claim_json")
        token=$(json_string lease_token "$claim_json")

        source_path=$SMB_ROOT/$source_rel
        local_part=$WORK_ROOT/$asset_id.mp4.part
        remote_final=$SMB_ROOT/$output_rel
        remote_part=$remote_final.part
        heartbeat_pid=

        fail_claim() {
            post fail "{\"asset_id\":\"$asset_id\",\"worker_id\":\"$worker_id\",\"lease_token\":\"$token\",\"phase\":\"failed\"}" >/dev/null 2>&1 || true
        }

        heartbeat_loop() {
            while :; do
                sleep 30
                post heartbeat "{\"asset_id\":\"$asset_id\",\"worker_id\":\"$worker_id\",\"lease_token\":\"$token\",\"phase\":\"processing\"}" >/dev/null 2>&1 || true
            done
        }

        if ! valid_relative_path "$source_rel" || ! valid_relative_path "$output_rel" || [ -z "$asset_id" ] || [ -z "$token" ]; then
            log failed "invalid-claim-or-source"
            fail_claim
            continue
        fi

        mkdir -p "$WORK_ROOT" "$(dirname "$remote_final")"
        log claimed "source=$source_rel audio=${audio_codec:-none}"
        heartbeat_loop &
        heartbeat_pid=$!

        if [ -f "$remote_final" ] && validate_output "$SMB_ROOT" "/work/$output_rel"; then
            post heartbeat "{\"asset_id\":\"$asset_id\",\"worker_id\":\"$worker_id\",\"lease_token\":\"$token\",\"phase\":\"verifying\"}" >/dev/null 2>&1 || true
            complete_response=$(post complete "{\"asset_id\":\"$asset_id\",\"worker_id\":\"$worker_id\",\"lease_token\":\"$token\",\"phase\":\"completed\"}" 2>&1) || {
                log failed "existing-output-complete response=$complete_response"
                kill "$heartbeat_pid" 2>/dev/null || true
                fail_claim
                continue
            }
            log completed "existing-output=$output_rel"
            kill "$heartbeat_pid" 2>/dev/null || true
            continue
        fi

        if [ ! -f "$source_path" ]; then
            log failed "source-missing"
            kill "$heartbeat_pid" 2>/dev/null || true
            fail_claim
            continue
        fi

        if [ "$audio_codec" = pcm_alaw ]; then
            audio_args='-c:a aac -profile:a aac_low -ac 1 -ar 8000 -b:a 32k'
        else
            audio_args='-c:a copy'
        fi

        # ponytail: one container per claimed asset; raise WORKERS only after SMB and CPU stay stable.
        if [ -s "$local_part" ]; then
            log reusing "existing-local-output"
        else
            # shellcheck disable=SC2086
            if ! "$DOCKER" run --rm --user 0:0 \
                -v "$SMB_ROOT:/mnt/a8:ro" \
                -v "$WORK_ROOT:/work" \
                "$IMAGE" -nostdin -hide_banner -loglevel warning -y \
                -copyts -start_at_zero -avoid_negative_ts disabled \
                -i "/mnt/a8/$source_rel" \
                -map 0:v:0 -c:v copy -tag:v hvc1 \
                -map 0:a:0? $audio_args \
                -fps_mode passthrough \
                -movflags +faststart -f mp4 "/work/$asset_id.mp4.part"; then
                log failed "ffmpeg"
                kill "$heartbeat_pid" 2>/dev/null || true
                fail_claim
                rm -f "$local_part"
                continue
            fi
        fi

        if ! validate_output "$WORK_ROOT" "/work/$asset_id.mp4.part"; then
            log failed "seek-validation"
            kill "$heartbeat_pid" 2>/dev/null || true
            fail_claim
            rm -f "$local_part"
            continue
        fi

        rm -f "$remote_part"
        if ! cp "$local_part" "$remote_part" || ! mv -f "$remote_part" "$remote_final"; then
            log failed "smb-copy"
            kill "$heartbeat_pid" 2>/dev/null || true
            rm -f "$remote_part" "$local_part"
            fail_claim
            continue
        fi
        rm -f "$local_part"

        post heartbeat "{\"asset_id\":\"$asset_id\",\"worker_id\":\"$worker_id\",\"lease_token\":\"$token\",\"phase\":\"verifying\"}" >/dev/null 2>&1 || true
        complete_response=$(post complete "{\"asset_id\":\"$asset_id\",\"worker_id\":\"$worker_id\",\"lease_token\":\"$token\",\"phase\":\"completed\"}" 2>&1) || {
            log failed "complete-api response=$complete_response"
            kill "$heartbeat_pid" 2>/dev/null || true
            fail_claim
            continue
        }
        log completed "output=$output_rel"
        kill "$heartbeat_pid" 2>/dev/null || true
    done
}

mkdir -p "$WORK_ROOT/logs"
i=1
while [ "$i" -le "$WORKERS" ]; do
    worker "qnap-normalize-$i" >"$WORK_ROOT/logs/worker-$i.log" 2>&1 &
    i=$((i + 1))
done
wait
