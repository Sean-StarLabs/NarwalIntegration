# v1.0.11

## Breaking

- The legacy suction select no longer advertises the duplicate `Super` and
  `Ultra` aliases. Select `Super Powerful` or `Ultra Powerful` instead.
  Vacuum `set_fan_speed` continues to accept `Super` and `Ultra` while docked;
  those aliases map to the highest level supported by the command path. During
  a clean, unsupported Ultra requests are rejected instead of being silently
  changed to another level.
