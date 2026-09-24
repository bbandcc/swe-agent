// Unicode byte-offset prefix: 雪 🧭
export interface Result<T> { value: T; }
export type Callback<T> = (value: T) => T;
export enum Mode { Read, Write }
export async function outer<T extends object>(value: T): Promise<T> {
  function nested<U extends T>(item: U): U { return item; }
  const arrow = <U extends T>(item: U): U => item;
  class Box<V> {
    async method<W extends V>(item: W): Promise<W> { return item; }
  }
  return value;
}
export class Worker<T> { method(value: T): T { return value; } }
