import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { EyeIcon, EyeOffIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSession } from "../../stores/session";
import { useAppTranslation } from "@/i18n";

const schema = z.object({
  username: z.string().min(1),
  pin: z.string().min(1),
  remember: z.boolean().optional(),
});
type FormValues = z.infer<typeof schema>;

const labelClass = "font-mono text-[11px] uppercase tracking-[0.09em] text-ink-secondary";
const inputClass =
  "h-11 w-full rounded-md border border-hairline bg-jack px-s4 font-mono text-[15px] text-ink-primary transition-colors duration-fast placeholder:text-ink-faint focus-visible:border-green focus-visible:outline-none";

/** Internal-team gate: username + PIN, lockout handled server-side. */
export function LoginScreen() {
  const login = useSession((s) => s.login);
  const { t } = useAppTranslation();
  const [showPin, setShowPin] = useState(false);
  const [serverError, setServerError] = useState("");

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { username: "", pin: "", remember: false },
  });

  const onSubmit = async (values: FormValues) => {
    setServerError("");
    try {
      await login(values.username.trim(), values.pin);
    } catch (err) {
      setServerError(err instanceof Error ? err.message : "login failed");
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
              zagent
            </span>
            <span className="led" aria-hidden="true" />
          </div>
          <span className="text-[13px] text-ink-secondary">{t("login.tagline")}</span>
        </div>

        <form
          onSubmit={handleSubmit(onSubmit)}
          className="flex flex-col gap-s5 rounded-lg border border-hairline bg-bg-panel p-s6"
          noValidate
        >
          <div className="flex flex-col gap-s2">
            <label htmlFor="login-username" className={labelClass}>
              {t("login.username")}
            </label>
            <input
              id="login-username"
              autoComplete="username"
              autoFocus
              aria-invalid={!!errors.username}
              aria-describedby={errors.username ? "login-username-err" : undefined}
              {...register("username")}
              className={inputClass}
            />
            {errors.username && (
              <p id="login-username-err" role="alert" className="font-mono text-[11.5px] text-danger-bright">
                {errors.username.message ?? t("login.errRequired")}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-s2">
            <label htmlFor="login-pin" className={labelClass}>
              {t("login.pin")}
            </label>
            <div className="relative">
              <input
                id="login-pin"
                type={showPin ? "text" : "password"}
                autoComplete="current-password"
                aria-invalid={!!errors.pin}
                aria-describedby={errors.pin ? "login-pin-err" : undefined}
                {...register("pin")}
                className={`${inputClass} pr-10`}
              />
              <button
                type="button"
                onClick={() => setShowPin((v) => !v)}
                aria-label={showPin ? t("login.hidePin") : t("login.showPin")}
                className="absolute right-s2 top-1/2 -translate-y-1/2 rounded-md p-1.5 text-ink-faint transition-colors duration-fast hover:text-ink-primary"
              >
                {showPin ? (
                  <EyeOffIcon className="size-4" aria-hidden="true" />
                ) : (
                  <EyeIcon className="size-4" aria-hidden="true" />
                )}
              </button>
            </div>
            {errors.pin && (
              <p id="login-pin-err" role="alert" className="font-mono text-[11.5px] text-danger-bright">
                {errors.pin.message ?? t("login.errPinLen")}
              </p>
            )}
          </div>

          <label className="flex cursor-pointer items-center gap-s2 text-[12.5px] text-ink-secondary">
            <input type="checkbox" {...register("remember")} className="size-4 accent-[var(--color-green)]" />
            {t("login.remember")}
          </label>

          {serverError && (
            <p role="alert" className="font-mono text-[12.5px] text-danger-bright">
              {serverError}
            </p>
          )}

          <Button type="submit" className="h-11 w-full gap-s2 font-mono text-[14px]" disabled={isSubmitting}>
            <span className="size-2 rounded-full bg-current" aria-hidden="true" />
            {isSubmitting ? t("login.submitting") : t("login.submit")}
          </Button>
        </form>

        <p className="text-center font-mono text-[11px] tracking-[0.04em] text-ink-faint">
          {t("login.footerOrg")}
        </p>
      </div>
    </div>
  );
}
