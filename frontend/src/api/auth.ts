import request from './request'

export interface LoginReq  { username: string; password: string }
export interface RegisterReq { username: string; password: string }
export interface TokenRes  { access_token: string; token_type: string }
export interface UserRes   { id: string; username: string; created_at: string }

export const authApi = {
  login:    (data: LoginReq):    Promise<TokenRes> => request.post('/auth/login', data),
  register: (data: RegisterReq): Promise<UserRes>  => request.post('/auth/register', data),
  me:       ():                  Promise<UserRes>  => request.get('/auth/me'),
}
