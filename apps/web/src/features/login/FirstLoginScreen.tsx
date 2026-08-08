import { useMemo, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useNavigate } from "react-router-dom";
import { toast } from "@/components/ui/sonner";
import { Button } from "@/components/ui/button";
import { api } from "../../lib/api";
import { useSession } from "../../stores/session";
import { CONSOLE_HOME } from "../../lib/routes";
import type { Me } from "../../types";

// W-H15: the only in-app redemption path for the one-time setup codes that
// TeamScreen mints. Calls /auth/first-login, which redeems the code, sets
// the chosen PIN, and returns a session cookie — then we hydrate /auth/me
// so the console opens already signed in.

type FormValues = {
  username: string;
  code: string;
  pin: string;
  confirmPin: string;
  display_name?: string;
};

const labelClass = "font-mono text-[11px] uppercase tracking-[0.09em] text-ink-secondary";
const inputClass =
  "h-11 w-full rounded-md border border-hairline bg-jack px-s4 font-mono text-[15px] text-ink-primary transition-colors duration-fast placeholder:text-ink-faint focus-visible:border-green focus-visible:outline-none";

export function FirstLoginScreen() {
  const navigate = useNavigate();
  const [serverError, setServerError] = useState("");

  const schema = useMemo(
    () =>
      z
        .object({
          username: z.string().min(1, "username is required"),
          code: z.string().regex(/^\d{8}$/, "the setup code is the 8 digits your admin sent"),
          pin: z.string().regex(/^\d{4,6}$/, "pin must be 4–6 digits"),
          confirmPin: z.string(),
          display_name: z.string().optional(),
        })
        .refine((v) => v.pin === v.confirmPin, { message: "pins don't match", path: ["confirmPin"] }),
    [],
  );

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { username: "", code: "", pin: "", confirmPin: "", display_name: "" },
  });

  const onSubmit = async (values: FormValues) => {
    setServerError("");
    try {
      await api.post("/auth/first-login", {
        username: values.username.trim(),
        code: values.code,
        pin: values.pin,
        display_name: values.display_name?.trim() || undefined,
      });
      const me = await api.get<Me>("/auth/me");
      useSession.setState({ me });
      toast.success(`welcome, ${me.display_name || me.username}`, {
        description: "your sessions are private to you — lessons you approve become shared team knowledge",
      });
      navigate(CONSOLE_HOME, { replace: true });
    } catch (err) {
      setServerError(err instanceof Error ? err.message : "setup failed");
    }
  };

  return (
    <div className="flex h-full items-center justify-center px-s4">
      <div className="flex w-[380px] max-w-full flex-col gap-s6">
        <div className="flex flex-col items-center gap-s1">
          <div className="flex items-center gap-s3">
            <span className="font-display text-[30px] font-semibold leading-none text-ok-bright" aria-hidden="true">
              ⌁
            </span>
            <span className="font-display text-[28px] font-semibold tracking-[0.01em] text-ink-primary">
              collegium
            </span>
            <span className="led" aria-hidden="true" />
          </div>
          <span className="text-[13px] text-ink-secondary">first-time setup — redeem your invite code</span>
        </div>

        <form
          onSubmit={handleSubmit(onSubmit)}
          className="flex flex-col gap-s5 rounded-lg border border-hairline bg-bg-panel p-s6"
          noValidate
        >
          <div className="flex flex-col gap-s2">
            <label htmlFor="fl-username" className={labelClass}>
              username
            </label>
            <input
              id="fl-username"
              autoComplete="username"
              autoFocus
              aria-invalid={!!errors.username}
              {...register("username")}
              className={inputClass}
            />
            {errors.username && (
              <p role="alert" className="font-mono text-[11.5px] text-danger-bright">
                {errors.username.message}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-s2">
            <label htmlFor="fl-code" className={labelClass}>
              setup code
            </label>
            <input
              id="fl-code"
              inputMode="numeric"
              autoComplete="one-time-code"
              aria-invalid={!!errors.code}
              {...register("code")}
              className={inputClass}
              placeholder="8 digits"
            />
            {errors.code && (
              <p role="alert" className="font-mono text-[11.5px] text-danger-bright">
                {errors.code.message}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-s2">
            <label htmlFor="fl-name" className={labelClass}>
              display name <span className="text-ink-faint">(optional)</span>
            </label>
            <input id="fl-name" {...register("display_name")} className={inputClass} />
          </div>

          <div className="flex flex-col gap-s2">
            <label htmlFor="fl-pin" className={labelClass}>
              choose a pin
            </label>
            <input
              id="fl-pin"
              type="password"
              inputMode="numeric"
              autoComplete="new-password"
              aria-invalid={!!errors.pin}
              {...register("pin")}
              className={inputClass}
              placeholder="4–6 digits"
            />
            {errors.pin && (
              <p role="alert" className="font-mono text-[11.5px] text-danger-bright">
                {errors.pin.message}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-s2">
            <label htmlFor="fl-pin2" className={labelClass}>
              repeat the pin
            </label>
            <input
              id="fl-pin2"
              type="password"
              inputMode="numeric"
              autoComplete="new-password"
              aria-invalid={!!errors.confirmPin}
              {...register("confirmPin")}
              className={inputClass}
            />
            {errors.confirmPin && (
              <p role="alert" className="font-mono text-[11.5px] text-danger-bright">
                {errors.confirmPin.message}
              </p>
            )}
          </div>

          {serverError && (
            <p role="alert" className="font-mono text-[12.5px] text-danger-bright">
              {serverError}
            </p>
          )}

          <Button type="submit" className="h-11 w-full gap-s2 font-mono text-[14px]" disabled={isSubmitting}>
            <span className="size-2 rounded-full bg-current" aria-hidden="true" />
            {isSubmitting ? "setting up…" : "set up & sign in"}
          </Button>

          <p className="text-center font-mono text-[11px] text-ink-faint">
            already have a pin?{" "}
            <a href="/login" className="text-ink-secondary underline underline-offset-2 hover:text-ink-primary">
              sign in
            </a>
          </p>
        </form>
      </div>
    </div>
  );
}
