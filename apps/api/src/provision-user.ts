/**
 * Provision an account from the command line.
 *
 *     npm run user:provision -w @leak/api -- --email a@b.co --name "Analyst"
 *
 * The password is read from PROVISION_PASSWORD, never from argv — arguments are visible to
 * every other process on the host via /proc and land in shell history.
 *
 * ## Why this exists
 *
 * `auth.ts` sets `disableSignUp: true`, so the public sign-up route is closed. That is the
 * right default for a threat-intel console, but it left the product with no way to create
 * any account at all: a fresh deployment had a login form and nothing that could log into
 * it. CI hit the same wall from the other side — `scripts/smoke-api.sh` used to sign up and
 * then sign in as the account it had just made, which stopped working the moment sign-up
 * was disabled.
 *
 * ## Why it goes through better-auth's context rather than writing rows
 *
 * Inserting into `user` and `account` directly would mean reproducing better-auth's password
 * encoding by hand. It hashes with scrypt and stores a format of its own choosing; a
 * hand-rolled hash that is subtly wrong produces a row that looks correct in the database
 * and fails only at sign-in. `auth.$context` exposes the same hasher and the same adapter
 * the sign-up route would have used, so this stays correct if that encoding ever changes.
 */
import { auth } from "./auth.js";

type Args = { email?: string; name?: string };

function parseArgs(argv: string[]): Args {
  const args: Args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--email") args.email = argv[++i];
    else if (flag === "--name") args.name = argv[++i];
  }
  return args;
}

function fail(message: string): never {
  console.error(`provision-user: ${message}`);
  process.exit(1);
}

async function main() {
  const { email, name } = parseArgs(process.argv.slice(2));
  const password = process.env.PROVISION_PASSWORD;

  if (!email) fail("--email is required");
  if (!password) fail("set PROVISION_PASSWORD (it is not taken as an argument on purpose)");

  // Matches `minPasswordLength` in auth.ts. Checked here so the failure is a clear message
  // rather than a row written that can never be signed in to.
  if (password.length < 12) fail("PROVISION_PASSWORD must be at least 12 characters");

  const ctx = await auth.$context;

  // Idempotent: re-running must not create a second account for the same address, and CI
  // re-runs this against a database that may already have been seeded.
  const existing = await ctx.internalAdapter.findUserByEmail(email);
  if (existing) {
    console.log(`provision-user: ${email} already exists — nothing to do`);
    return;
  }

  const created = await ctx.internalAdapter.createUser({
    email,
    name: name ?? email,
    // No mail is sent by this console, so an address that can never be confirmed would
    // otherwise block sign-in when `requireEmailVerification` is later turned on.
    emailVerified: true,
  });

  await ctx.internalAdapter.createAccount({
    userId: created.id,
    providerId: "credential",
    accountId: created.id,
    password: await ctx.password.hash(password),
  });

  console.log(`provision-user: created ${email}`);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error("provision-user: failed", error);
    process.exit(1);
  });
