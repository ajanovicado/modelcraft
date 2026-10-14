export type AsyncActionStatus = "idle" | "pending" | "success" | "error";
export type AsyncActionToken = Readonly<{ scope: string }>;

/** A synchronous admission guard; completion belongs only to its original form. */
export class AsyncActionLifetime {
  private active?: AsyncActionToken;

  begin(scope: string): AsyncActionToken | undefined {
    if (this.active) return undefined;
    const token = { scope };
    this.active = token;
    return token;
  }

  owns(token: AsyncActionToken, scope: string): boolean {
    return this.active === token && token.scope === scope;
  }

  release(token: AsyncActionToken): boolean {
    if (this.active !== token) return false;
    this.active = undefined;
    return true;
  }

  invalidate(): void {
    this.active = undefined;
  }
}
