#!/usr/bin/env bash

read_env_value() {
  local file="$1"
  local key="$2"
  local fallback="${3:-}"
  local value

  value=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" == "$key="* ]]; then
      value="${line#*=}"
      break
    fi
  done < "$file"

  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi

  printf '%s' "${value:-$fallback}"
}
