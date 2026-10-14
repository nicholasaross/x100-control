# Optional: Alexa through your own AWS account

The voice listener on the Raspberry Pi is the main way to control the projector, with nothing
leaving the house (see the [main README](../README.md)). This folder keeps the earlier Alexa route,
for if you also want "Alexa, turn on the projector" on your Echo devices. It stays off unless
`AWS_IOT_ENDPOINT` is set in the bridge's `bridge.env`, and both routes can run at once.

```
Echo ─► Alexa ─► Lambda (your AWS account, eu-west-1)
                   │  publish command              ▲ read ack + state
                   ▼                               │
                 AWS IoT Core  ◄── MQTT over TLS, outbound only ──►  x100-bridge (on the Pi)
```

Expect roughly 1.5–3 seconds per command this way (Amazon's speech recognition plus the round trip
through AWS), against about half a second for the local listener.

* **No inbound ports.** The bridge makes one outbound TLS connection to AWS IoT with its own certificate.
* **Honest answers.** The bridge acknowledges every command, so if the projector is unplugged Alexa says
  so rather than "OK".
* **Narrow permissions.** The bridge certificate can only use its own command topic and device shadow,
  and only your Alexa skill can invoke the Lambda. Costs pennies a month.

## What you can say

| Say | Does |
|---|---|
| "Alexa, turn on / off the projector" | Power, including from standby |
| "Alexa, switch the projector to HDMI 2" | Input; turns the projector on first if needed |
| "Alexa, set the projector volume to 20" | Volume, on the projector's own 0–50 scale |
| "Alexa, turn the projector up / down" | ±3 (`VOLUME_STEP`), or "…down by 10" |
| "Alexa, mute / unmute the projector" | Mute (the projector only mutes while showing an input) |

## What you need

* The bridge already working: on the Pi that does everything (main README), or on another machine.
* An AWS account with the AWS CLI v2 configured, and an Amazon developer account signed in with
  **the same Amazon account your Echo devices use** (development skills only appear for that account).
* About 45 minutes.

## 1. Create the Alexa skill

In the [Alexa developer console](https://developer.amazon.com/alexa/console/ask) (Amazon now calls
smart home skills "add-ons"; the console still says skill):

1. **Create Skill**. Name: anything, e.g. *X100 Projector*. Primary locale: **English (UK)**.
2. Experience: **Smart Home**; model: **Smart Home**; hosting: **Provision your own**. Create.
3. On the Smart Home page, copy **Your Skill ID** (`amzn1.ask.skill.…`).

## 2. Deploy the AWS side

Run this wherever your AWS CLI is set up (your Mac is fine), from the unpacked project folder:

```bash
aws sts get-caller-identity            # check you're in the right account
alexa/infra/deploy.sh amzn1.ask.skill.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

This runs in **eu-west-1 (Ireland)**, where English (UK) Alexa add-ons must be hosted. It:

* generates the bridge's private key **locally** and has AWS IoT issue a certificate from a CSR, so the
  key never leaves your machine;
* creates the IoT thing, a policy scoped to that one thing, the Lambda and an invoke permission that
  only your skill ID can use (CloudFormation stack `x100-alexa`);
* writes `certs/` and a `bridge.env` with the AWS settings, next to it on your Mac. Step 4 moves
  them to the bridge.

It prints the Lambda ARN. Back in the Alexa console, on the Smart Home page, paste it as the
**Default endpoint**, tick **Europe, India** and paste the same ARN there, then **Save**.

Re-running `deploy.sh` is safe: it reuses the certificate and updates the stack. Options:
`FRIENDLY_NAME="Cinema projector"`, `AWS_PROFILE=…`, `STACK_NAME=…`, `ALLOWED_USER_ID=amzn1.account.…`
(pins the Lambda to your Amazon account; a wrong value logs the real caller's ID in CloudWatch).

## 3. Account linking (Login with Amazon)

Smart home skills must have account linking. For a personal skill, Login with Amazon is the simplest:

1. [Login with Amazon console](https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html)
   → **Create a New Security Profile**: any name and description, any privacy URL. Save, then
   **Show Client ID and Client Secret**.
2. Alexa console → your skill → **Account Linking** (under *Build*):
   * Grant type: **Auth Code Grant**
   * Web Authorization URI: `https://www.amazon.com/ap/oa`
   * Access Token URI: `https://api.amazon.com/auth/o2/token`
   * Client ID / Secret: from step 1
   * Authentication scheme: **HTTP Basic (Recommended)**
   * Scope: `profile:user_id`
   * **Save**, then copy the three **Alexa Redirect URLs** at the bottom of the page.
3. Back in Login with Amazon: your security profile → gear icon → **Web Settings** → **Edit** → add the
   three URLs under **Allowed Return URLs** → **Save**.

## 4. Turn the link on in the bridge

**On the Pi that does everything** (the usual layout), copy the certificates across:

```bash
scp -r certs x100-controller@x100-pi.local:
ssh x100-controller@x100-pi.local
sudo mv ~/certs /opt/x100-control/
sudo chown -R root:x100 /opt/x100-control/certs
sudo chmod 750 /opt/x100-control/certs && sudo chmod 640 /opt/x100-control/certs/*
```

Then add the five AWS lines to the Pi's settings. Don't copy the whole file: the Pi's own
`bridge.env` holds its serial port and token.

```bash
sudo nano /opt/x100-control/bridge.env
#   THING_NAME, AWS_IOT_ENDPOINT, CERT_FILE, KEY_FILE and CA_FILE, as in the bridge.env on your Mac
cd ~/x100-control && sudo deploy/install.sh --alexa
```

`--alexa` adds the MQTT library and lets the bridge reach AWS. The bridge is otherwise firewalled to
your local network; the listener stays firewalled either way. Then check it:

```bash
cd /opt/x100-control
sudo systemctl stop x100-bridge
sudo -u x100 .venv/bin/x100-bridge --env-file bridge.env --check    # ends "connected and published"
sudo systemctl start x100-bridge
```

**With the bridge in Docker** (the LAN layout): copy `certs/` and the AWS lines into the project folder
on that machine. In `docker-compose.yml` set `EXTRAS: "aws"`, uncomment the `./certs:/certs:ro`
volume and the three certificate paths, then run `docker compose up -d --build`.

Either way the local listener keeps working alongside Alexa.

## 5. Link and discover

1. Alexa app → **More → Skills & Games → Your Skills → Dev** → *X100 Projector* → **Enable to use** →
   sign in with Amazon.
2. Let it **discover devices**. A device called **Projector** appears.
3. If the old ViewSonic skill is still enabled, disable it and delete its stale projector device.
4. "Alexa, turn on the projector."

After any change to `config/inputs.json` or `X100_SCENES`, restart the bridge and say "Alexa,
discover devices".

## Focus and other remote keys

The X100-4K's auto-focus is an IR-remote key (`0x92`). ViewSonic's LAN protocol has a generic
"remote key" command but doesn't promise to accept every IR code, so test first:
`x100ctl key 0x92`. If it works, expose it as an Alexa scene in `bridge.env`:

```
X100_SCENES={"Projector Focus": "0x92"}
```

Restart the bridge and rediscover; "Alexa, turn on Projector Focus" then works (or make a routine for
"focus the projector"). All the IR codes are in `x100/protocol.py` (`X100_IR_CODES`). The local
listener can send the same key: add `"projector focus": [{"op": "key", "value": 146}]` to
`EXTRA_PHRASES`.

## Troubleshooting

| Symptom | Check |
|---|---|
| "The projector isn't responding" for everything | Is the bridge running with the AWS link? `docker compose logs`. The Lambda treats the bridge as offline after 3 minutes without a heartbeat. |
| Account linking fails or loops | The three Alexa redirect URLs must be in the security profile's Allowed Return URLs; scope must be `profile:user_id`. |
| Bridge logs "command … is from the future" or drops commands as old | The bridge machine's clock is wrong; enable NTP. |
| Anything else | Lambda logs: CloudWatch `/aws/lambda/x100-projector-alexa`. Bridge: `LOG_LEVEL=DEBUG`. |

## Uninstall

`alexa/infra/teardown.sh` deletes the stack, revokes and deletes the bridge certificate and removes the
artifact bucket. Then delete the skill in the Alexa console and the security profile in Login with
Amazon, and unset `AWS_IOT_ENDPOINT` in `bridge.env`.
