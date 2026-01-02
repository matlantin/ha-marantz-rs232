# Marantz RS232 Media Player for Home Assistant

This custom component integrates Marantz receivers (tested with SR6001) into Home Assistant via an RS232 serial connection. It provides a robust `media_player` entity with support for power, volume, mute, and source selection.

## Features

-   **Power Control**: Turn the receiver on and off.
-   **Volume Control**:
    -   Set volume level (absolute dB support).
    -   Volume Up/Down steps.
    -   Mute/Unmute.
    -   **Smart Volume Handling**: Includes debouncing and locking to ensure smooth slider operation in the UI, preventing "jumping" values during polling.
-   **Source Selection**: Switch between input sources.
-   **Robust Serial Communication**:
    -   Automatic reconnection and error handling.
    -   Thread-safe serial operations.
    -   Configurable polling interval.
-   **Customizable Commands**: Define your own RS232 commands via YAML configuration or use the built-in defaults.
-   **Debugging**: `send_raw` service to send arbitrary commands for testing.

## Installation

### Option 1: Via HACS (Recommended)
1.  Open HACS in Home Assistant.
2.  Go to "Integrations" > Top right menu > "Custom repositories".
3.  Add the URL of this repository.
4.  Category: "Integration".
5.  Click "Add" and then "Download".
6.  Restart Home Assistant.

### Option 2: Manual Installation
1.  Download the `marantz_rs232` folder from this repository.
2.  Copy it to your Home Assistant `custom_components` directory (e.g., `/config/custom_components/marantz_rs232`).
3.  Restart Home Assistant.

## Configuration

### Method 1: UI Configuration (Basic)
After installation and restart:
1.  Go to **Settings** > **Devices & Services**.
2.  Click **Add Integration**.
3.  Search for "Marantz RS232".
4.  Enter your Serial Port (e.g., `/dev/ttyUSB0`), Baudrate, and Name.

*Note: UI configuration uses default settings (`optimistic: false`, `use_marantzusb_format: true`).*

### Method 2: YAML Configuration (Advanced)
To customize advanced parameters like `optimistic` mode or `command_map`, use `configuration.yaml`. This works for both HACS and manual installations.

Add the following to your `configuration.yaml`:

```yaml
media_player:
  - platform: marantz_rs232
    name: "Marantz Receiver"
    serial_port: /dev/ttyUSB0  # Update with your serial port
    baudrate: 9600             # Default is 9600
    poll_interval: 10          # Polling interval in seconds
    optimistic: false          # Set to true for immediate UI updates without waiting for device confirmation
    use_marantzusb_format: true # Use specific volume command format (@VOL:0+...)
    
    # Optional: Override or add commands
    command_map:
      power_on: "@PWR:2"
      power_off: "@PWR:1"
      volume_up: "@VOL:1"
      volume_down: "@VOL:2"
      mute_on: "@AMT:2"
      mute_off: "@AMT:1"
      query_power: "@PWR:?"
      query_volume: "@VOL:?"
      query_source: "@SRC:?"
      sources:
        TV: "@SRC:C"
        DVD: "@SRC:D"
        Tuner: "@SRC:2"
```

### Command Map

The integration uses a `command_map` to define the RS232 strings sent to the device. If not provided in the configuration, it attempts to load defaults from `command_map_parsed.yaml` included in the component.

Key commands include:
-   `power_on`, `power_off`
-   `volume_up`, `volume_down`
-   `mute_on`, `mute_off`
-   `query_power`, `query_volume`, `query_source`
-   `sources`: A dictionary mapping source names to commands.

## Services

### `marantz_rs232.send_raw`

Sends a raw RS232 command to the receiver. Useful for testing or advanced usage.

**Parameters:**
-   `command` (Required): The raw string to send (e.g., `@PWR:?`).
-   `entity_id` (Optional): The entity ID to target.

**Example:**
```yaml
service: marantz_rs232.send_raw
data:
  command: "@VOL:?"
  entity_id: media_player.marantz_receiver
```

## Troubleshooting

-   **Logs**: Enable debug logging for this component to see detailed serial communication.
    ```yaml
    logger:
      default: info
      logs:
        custom_components.marantz_rs232: debug
    ```
-   **Serial Port**: Ensure the user running Home Assistant has permission to access the serial port.
